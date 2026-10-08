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


def _template_id(data: dict) -> str:
    """Identifiant métier d'un template ancien : obligatoire (le moteur en génèrerait un au hasard à chaque chargement) et
    utilisable comme nom de fichier ``<id>.json`` (aucun séparateur de chemin, ni ``.``/``..``)."""
    template_id = data.get("id")
    if not isinstance(template_id, str) or not template_id.strip():
        raise ValueError("« id » manquant ou vide")
    if (
        template_id in {".", ".."}
        or any(c in template_id for c in ("/", "\\", "\x00"))
        or template_id != template_id.strip()
    ):
        raise ValueError(f"« id » inutilisable comme nom de fichier : {template_id!r}")
    return template_id


def _version_entries_problem(entries: Any) -> Optional[str]:
    """None si ``entries`` satisfait le contrat du chargeur (``PromptVersionManager._load_versions``) : une liste d'objets
    acceptés par ``PromptVersion.from_dict``. Sinon la raison, car le chargeur vide TOUT son cache sur une seule entrée
    invalide (puis la sauvegarde suivante supprimerait l'historique sain)."""
    from .engine.versioning import PromptVersion

    if not isinstance(entries, list):
        return f"liste attendue, {type(entries).__name__} reçu"
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return f"élément {index}: objet attendu, {type(entry).__name__} reçu"
        try:
            PromptVersion.from_dict(entry)
        except Exception as exc:  # noqa: BLE001
            return f"élément {index}: {type(exc).__name__}: {exc}"
    return None


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

    # 1) templates : l'IDENTIFIANT métier protège le nouvel état, quel que soit le nom de fichier ---------
    dest_by_id: dict[str, tuple[str, dict]] = {}
    dest_names: dict[str, str] = {}
    if templates_dest.is_dir():
        for existing in sorted(templates_dest.glob("*.json")):
            try:
                data = _read_json(existing)
                dest_by_id.setdefault(str(data["id"]), (existing.name, data))
                dest_names[str(data.get("name"))] = str(data["id"])
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                continue  # fichier du nouvel état illisible ou sans id : jamais touché ; le moteur ne le charge pas non plus
    batch_ids: dict[str, str] = {}  # id -> fichier source déjà retenu dans ce lot
    for path in sorted(templates_src.glob("*.json")) if templates_src.is_dir() else []:
        try:
            raw = path.read_bytes()
            data = _validate_template(json.loads(raw.decode("utf-8")))
            template_id = _template_id(data)
        except Exception as exc:  # noqa: BLE001 - tout fichier invalide est consigné, jamais ignoré en silence
            report.errors.append(f"templates/{path.name}: {type(exc).__name__}: {exc}")
            continue
        if template_id in dest_by_id:
            existing_name, existing_data = dest_by_id[template_id]
            if existing_data == data:
                report.identical += 1
            else:
                report.conflicts.append(
                    f"template {template_id} ({path.name}): id déjà présent dans le nouvel état ({existing_name}), conservé"
                )
            continue
        if template_id in batch_ids:
            report.conflicts.append(
                f"template {template_id} ({path.name}): même id que {batch_ids[template_id]} déjà retenu, "
                "laissé dans l'ancien dossier"
            )
            continue
        name = str(data.get("name"))
        if name in dest_names and dest_names[name] != template_id:
            report.conflicts.append(f"template {template_id}: nom « {name} » déjà présent dans le nouvel état")
            continue
        target = templates_dest / f"{template_id}.json"  # le moteur retrouve/supprime un template par <id>.json
        if target.exists():
            report.conflicts.append(f"template {template_id} ({path.name}): {target.name} existe déjà, conservé")
            continue
        batch_ids[template_id] = path.name
        if write:
            _atomic_write(target, raw)
        report.templates += 1

    # 2) catégories : ajout des identifiants absents, entrée par entrée ---------------------------------
    if categories_src.is_file():
        legacy_categories: dict = {}
        try:
            loaded = _read_json(categories_src)
            if not isinstance(loaded, dict):
                raise ValueError("objet JSON attendu")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"templates/categories.json: {type(exc).__name__}: {exc}")
        else:
            from .engine.models import PromptCategory

            for cat_id, cat in loaded.items():
                try:
                    if not isinstance(cat, dict):
                        raise TypeError("objet JSON attendu")
                    PromptCategory(**cat)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(
                        f"templates/categories.json: catégorie « {cat_id} » invalide ({type(exc).__name__}: {exc})"
                    )
                else:
                    legacy_categories[cat_id] = cat
            current: Optional[dict]
            if categories_dest.is_file():
                try:
                    current = _read_json(categories_dest)
                    if not isinstance(current, dict):
                        raise ValueError("objet JSON attendu")
                except (OSError, ValueError) as exc:
                    current = None
                    report.errors.append(f"categories.json (nouvel état) illisible, non modifié: {exc}")
            else:
                from .storage import seed_categories_file

                current = _read_json(seed_categories_file())
            if current is not None:
                additions = {k: v for k, v in legacy_categories.items() if k not in current}
                report.conflicts += [
                    f"catégorie {k}: le nouvel état est conservé"
                    for k in legacy_categories
                    if k in current and current[k] != legacy_categories[k]
                ]
                if additions:
                    if write:
                        merged = {**current, **additions}
                        _atomic_write(categories_dest, json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"))
                    report.category_ids += len(additions)

    # 3) historique de versions : contrat RÉEL du chargeur, validé avant toute publication ----------------
    if versions_src.is_file():
        try:
            loaded_versions = _read_json(versions_src)
            if not isinstance(loaded_versions, dict):
                raise ValueError("objet JSON attendu")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"versions/versions.json: {type(exc).__name__}: {exc}")
        else:
            legacy_versions: dict = {}
            for key, entries in loaded_versions.items():
                problem = _version_entries_problem(entries)
                if problem:
                    report.errors.append(f"versions/versions.json: clé « {key} » invalide ({problem})")
                else:
                    legacy_versions[key] = entries
            current_versions: Optional[dict] = {}
            if versions_dest.is_file():
                try:
                    current_versions = _read_json(versions_dest)
                    if not isinstance(current_versions, dict):
                        raise ValueError("objet JSON attendu")
                    for key, entries in current_versions.items():
                        problem = _version_entries_problem(entries)
                        if problem:
                            raise ValueError(f"clé « {key} » invalide ({problem})")
                except Exception as exc:  # noqa: BLE001
                    current_versions = None
                    report.errors.append(f"versions.json (nouvel état) invalide, non modifié: {exc}")
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
