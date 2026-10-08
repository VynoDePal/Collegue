"""Reprise de l'ancien état de prompts (stocké DANS le paquet) vers ``$COLLEGUE_HOME/prompts``.

Avant la vague 2, le moteur de prompts écrivait son état dans le répertoire du paquet :

    <paquet>/prompts/templates/categories.json         catégories (graines + personnalisées)
    <paquet>/prompts/templates/templates/<id>.json     templates créés à l'exécution
    <paquet>/prompts/versions/versions.json            historique de versions

Depuis, ce répertoire est en lecture seule et l'état vit sous ``$COLLEGUE_HOME/prompts``. Sans reprise, une mise à jour en
place « perdait » les personnalisations. Ce module les reprend :

- **automatiquement** au démarrage quand le stockage par défaut est utilisé (pas d'override explicite) : la source est le
  répertoire ``prompts`` du paquet courant (cas d'une mise à jour en place) ;
- **explicitement** pour une ancienne installation située ailleurs :
  ``python -m collegue.prompts.legacy import --from <ancienne installation> [--dry-run]``.

Garanties :

- la source n'est **jamais** modifiée ni supprimée (lecture seule : le paquet peut être en lecture seule) ;
- le nouvel état n'est **jamais écrasé** : en cas de conflit (même id de template, même clé d'historique, même nom
  de template, même id de catégorie) le nouvel état gagne et le conflit est consigné ;
- chaque fichier est publié de façon atomique (écriture temporaire puis renommage) : une interruption ne laisse aucun
  fichier tronqué, et la reprise suivante termine le travail sans doublon ;
- un fichier ancien invalide est **signalé** (journal ERROR + marqueur) et laissé intact ; la reprise reste « incomplète »
  et sera retentée au prochain démarrage ; les fichiers valides sont repris quand même ;
- le marqueur ``.legacy-import.json`` (sous le nouvel état) ne passe à ``complete`` qu'une fois tout repris sans erreur :
  les redémarrages suivants ne rejouent rien (une suppression volontaire dans le nouvel état n'est pas ressuscitée).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

MARKER_NAME = ".legacy-import.json"
LOCK_NAME = ".legacy-import.lock"
MARKER_VERSION = 1

_process_attempted: set[tuple[str, str]] = set()
_process_lock = threading.Lock()


@dataclass
class ImportReport:
    source: Path
    dest: Path
    found: bool = False  # un ancien état existe à la source
    already_done: bool = False
    dry_run: bool = False
    templates: int = 0
    category_ids: int = 0
    version_keys: int = 0
    identical: int = 0
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors

    def imported(self) -> dict[str, int]:
        return {"templates": self.templates, "categories": self.category_ids, "version_keys": self.version_keys}

    def summary(self) -> str:
        verb = "à reprendre" if self.dry_run else "repris"
        return (
            f"{self.templates} template(s), {self.category_ids} catégorie(s), {self.version_keys} clé(s) d'historique {verb} "
            f"depuis {self.source} ; {self.identical} identique(s), {len(self.conflicts)} conflit(s), "
            f"{len(self.errors)} erreur(s)"
        )


# --- localisation ------------------------------------------------------------------------------------


def package_prompts_dir() -> Path:
    """Répertoire ``prompts`` du paquet courant : l'ancien emplacement d'une mise à jour en place."""
    return Path(__file__).resolve().parent


def resolve_source(path: Path) -> Optional[Path]:
    """Accepte le répertoire ``prompts``, la racine d'un paquet ``collegue`` ou celle d'un dépôt."""
    path = Path(path).expanduser()
    for candidate in (path, path / "prompts", path / "collegue" / "prompts"):
        if (candidate / "templates").is_dir() or (candidate / "versions").is_dir():
            return candidate.resolve()
    return None


def _legacy_paths(source: Path) -> tuple[Path, Path, Path]:
    return (
        source / "templates" / "templates",
        source / "templates" / "categories.json",
        source / "versions" / "versions.json",
    )


def _differs_from_seed(categories_file: Path) -> bool:
    """Un ``categories.json`` n'est de l'état que s'il diffère des catégories livrées (sinon c'est la graine elle-même)."""
    from .storage import seed_categories_file

    try:
        return _read_json(categories_file) != _read_json(seed_categories_file())
    except (OSError, ValueError):
        return True  # illisible : à signaler par la reprise plutôt qu'à ignorer


def has_legacy_state(source: Path) -> bool:
    """Vrai si ``source`` contient de l'état à reprendre (templates JSON, historique, catégories ≠ graines)."""
    templates, categories, versions = _legacy_paths(source)
    if templates.is_dir() and any(templates.glob("*.json")):
        return True
    if versions.is_file():
        return True
    return categories.is_file() and _differs_from_seed(categories)


# --- primitives sûres ----------------------------------------------------------------------------------


def _publish(tmp: Path, dest: Path) -> None:
    """Publie ``tmp`` sous le nom ``dest`` de façon atomique (renommage)."""
    os.replace(tmp, dest)


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _publish(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Verrou inter-processus (POSIX) ; sans ``fcntl`` la reprise reste correcte (publication atomique, sans écrasement)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - plateformes sans fcntl
        yield
        return
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_template(data: Any) -> dict:
    """Même validation que le moteur (``PromptEngine._load_library``) : un template invalide serait ignoré au chargement."""
    from .engine.models import PromptTemplate, PromptVariable

    if not isinstance(data, dict):
        raise ValueError("le fichier ne contient pas un objet JSON")
    candidate = dict(data)
    if "variables" in candidate:
        candidate["variables"] = [PromptVariable(**v) if isinstance(v, dict) else v for v in candidate["variables"]]
    PromptTemplate(**candidate)
    return data


def _load_marker(path: Path) -> dict:
    if not path.is_file():
        return {"version": MARKER_VERSION, "sources": {}}
    try:
        data = _read_json(path)
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            return data
    except (OSError, ValueError):
        pass
    # Marqueur illisible : on repart de zéro (la reprise ne remplace jamais l'existant, donc sans risque).
    logger.warning("Marqueur de reprise des prompts illisible (%s) : reprise rejouée sans écrasement", path)
    return {"version": MARKER_VERSION, "sources": {}}


# --- reprise -------------------------------------------------------------------------------------------


def import_legacy_state(source: Path, *, dest: Optional[Path] = None, dry_run: bool = False) -> ImportReport:
    """Reprend l'ancien état de ``source`` (répertoire ``prompts``) dans ``dest`` (défaut : ``$COLLEGUE_HOME/prompts``)."""
    from .storage import default_storage_dir

    dest = Path(dest) if dest is not None else default_storage_dir()
    source = Path(source).resolve()
    report = ImportReport(source=source, dest=dest, dry_run=dry_run)
    report.found = has_legacy_state(source)
    if not report.found or source == dest.resolve():
        return report

    marker_path = dest / MARKER_NAME
    key = str(source)
    if not dry_run:
        with _exclusive(dest / LOCK_NAME):
            marker = _load_marker(marker_path)
            if marker["sources"].get(key, {}).get("status") == "complete":
                report.already_done = True
                return report
            _import_locked(report, marker, marker_path)
    else:
        marker = _load_marker(marker_path)
        if marker["sources"].get(key, {}).get("status") == "complete":
            report.already_done = True
            return report
        _import_locked(report, marker, marker_path)
    return report


def _import_locked(report: ImportReport, marker: dict, marker_path: Path) -> None:
    source, dest = report.source, report.dest
    templates_src, categories_src, versions_src = _legacy_paths(source)
    templates_dest = dest / "templates"
    versions_dest = dest / "versions" / "versions.json"
    categories_dest = dest / "categories.json"
    write = not report.dry_run

    # 1) templates -------------------------------------------------------------------------------
    dest_names: dict[str, str] = {}
    if templates_dest.is_dir():
        for existing in templates_dest.glob("*.json"):
            try:
                data = _read_json(existing)
                dest_names[str(data.get("name"))] = str(data.get("id", existing.stem))
            except (OSError, ValueError, AttributeError):
                continue  # un fichier illisible du nouvel état n'est pas notre affaire et n'est jamais touché
    for path in sorted(templates_src.glob("*.json")) if templates_src.is_dir() else []:
        try:
            raw = path.read_bytes()
            data = _validate_template(json.loads(raw.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - tout fichier invalide est consigné, jamais ignoré en silence
            report.errors.append(f"templates/{path.name}: {type(exc).__name__}: {exc}")
            continue
        target = templates_dest / path.name
        if target.exists():
            if target.read_bytes() == raw:
                report.identical += 1
            else:
                report.conflicts.append(f"template {data.get('id', path.stem)}: le nouvel état est conservé")
            continue
        name, template_id = str(data.get("name")), str(data.get("id", path.stem))
        if name in dest_names and dest_names[name] != template_id:
            report.conflicts.append(f"template {template_id}: nom « {name} » déjà présent dans le nouvel état")
            continue
        if write:
            _atomic_write(target, raw)
        report.templates += 1

    # 2) catégories : ajout des identifiants absents -------------------------------------------------
    if categories_src.is_file():
        try:
            legacy_categories = _read_json(categories_src)
            if not isinstance(legacy_categories, dict):
                raise ValueError("objet JSON attendu")
            from .engine.models import PromptCategory

            for cat in legacy_categories.values():
                PromptCategory(**cat)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"templates/categories.json: {type(exc).__name__}: {exc}")
        else:
            if categories_dest.is_file():
                try:
                    current = _read_json(categories_dest)
                except (OSError, ValueError) as exc:
                    current = None
                    report.errors.append(f"categories.json (nouvel état) illisible, non modifié: {exc}")
            else:
                from .storage import seed_categories_file

                current = _read_json(seed_categories_file())
            if isinstance(current, dict):
                additions = {k: v for k, v in legacy_categories.items() if k not in current}
                same = [k for k in legacy_categories if k in current and current[k] != legacy_categories[k]]
                report.conflicts += [f"catégorie {k}: le nouvel état est conservé" for k in same]
                if additions:
                    if write:
                        merged = {**current, **additions}
                        _atomic_write(categories_dest, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"))
                    report.category_ids += len(additions)

    # 3) historique de versions : fusion par clé de premier niveau -------------------------------
    if versions_src.is_file():
        try:
            legacy_versions = _read_json(versions_src)
            if not isinstance(legacy_versions, dict):
                raise ValueError("objet JSON attendu")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"versions/versions.json: {type(exc).__name__}: {exc}")
        else:
            current_versions: Optional[dict] = {}
            if versions_dest.is_file():
                try:
                    current_versions = _read_json(versions_dest)
                    if not isinstance(current_versions, dict):
                        raise ValueError("objet JSON attendu")
                except Exception as exc:  # noqa: BLE001
                    current_versions = None
                    report.errors.append(f"versions.json (nouvel état) illisible, non modifié: {exc}")
            if current_versions is not None:
                additions = {k: v for k, v in legacy_versions.items() if k not in current_versions}
                report.conflicts += [
                    f"historique {k}: le nouvel état est conservé"
                    for k in legacy_versions
                    if k in current_versions and current_versions[k] != legacy_versions[k]
                ]
                if additions:
                    if write:
                        merged = {**current_versions, **additions}
                        _atomic_write(versions_dest, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"))
                    report.version_keys += len(additions)

    # 4) marqueur — en dernier : jamais « complete » tant qu'une erreur subsiste ---------------------
    if write:
        marker["version"] = MARKER_VERSION
        marker["sources"][str(source)] = {
            "status": "complete" if report.complete else "incomplete",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "imported": report.imported(),
            "identical": report.identical,
            "conflicts": report.conflicts,
            "errors": report.errors,
        }
        _atomic_write(marker_path, json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"))

    _log(report)


def _log(report: ImportReport) -> None:
    if report.errors:
        logger.error(
            "Reprise de l'ancien état de prompts INCOMPLÈTE (%s) : %s — fichiers anciens laissés intacts, reprise retentée "
            "au prochain démarrage. Erreurs : %s",
            report.source,
            report.summary(),
            "; ".join(report.errors),
        )
    elif report.conflicts:
        logger.warning(
            "Reprise de l'ancien état de prompts avec conflits (nouvel état conservé) : %s", report.summary()
        )
    elif report.templates or report.category_ids or report.version_keys:
        logger.info("Ancien état de prompts repris : %s", report.summary())


def ensure_default_import() -> Optional[ImportReport]:
    """Reprise automatique (stockage par défaut uniquement) de l'état du paquet courant, une fois par processus.

    Ne lève jamais : une panne de reprise est journalisée en ERROR, l'ancien état reste intact et sera retenté.
    """
    from .storage import default_storage_dir

    source = package_prompts_dir()
    dest = default_storage_dir()
    attempt = (str(source), str(dest))
    with _process_lock:
        if attempt in _process_attempted:
            return None
        _process_attempted.add(attempt)
    try:
        return import_legacy_state(source, dest=dest)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Reprise de l'ancien état de prompts impossible (%s) : ancien état laissé intact, nouvel essai au prochain démarrage",
            source,
        )
        return None


# --- CLI -----------------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m collegue.prompts.legacy", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="reprend l'état de prompts d'une ancienne installation")
    imp.add_argument(
        "--from", dest="source", required=True, help="ancienne installation (dossier prompts, paquet collegue ou dépôt)"
    )
    imp.add_argument("--dry-run", action="store_true", help="n'écrit rien : annonce ce qui serait repris")
    args = parser.parse_args(argv)

    source = resolve_source(Path(args.source))
    if source is None:
        print(
            f"ERROR: ancienne installation introuvable (ni templates/ ni versions/) sous {args.source}", file=sys.stderr
        )
        return 2
    report = import_legacy_state(source, dry_run=args.dry_run)
    if not report.found:
        print(f"Aucun ancien état de prompts à reprendre sous {source}.")
        return 0
    if report.already_done:
        print(f"Ancien état déjà repris (marqueur {report.dest / MARKER_NAME}) : rien à faire pour {source}.")
        return 0
    print(("[dry-run] " if args.dry_run else "") + report.summary())
    for conflict in report.conflicts:
        print(f"CONFLIT: {conflict}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
