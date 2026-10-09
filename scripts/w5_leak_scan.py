#!/usr/bin/env python3
"""Scanner de confiance : aucun fichier publié en artefact ne contient la clé du fournisseur (propriété C, vague 5).

La clé n'existe que dans l'environnement de l'étape de campagne ; ce script y est lancé APRÈS la campagne. Il fait deux choses indissociables :

* il cherche la clé (valeur lue dans la variable d'environnement nommée par ``--env``, jamais dans l'argv ni un fichier ; jamais imprimée)
  dans TOUT : le contenu de chaque fichier **en octets** (binaires, registre SQLite, journaux) par blocs avec recouvrement, ET les noms
  de fichiers, de répertoires et de liens ; formes cherchées : brute, URL, JSON, base64 (trois alignements) ;
* il construit le SEUL ensemble publiable (``--stage``) : un fichier n'y entre que si ses octets ont été lus, vérifiés ET copiés dans le
  même passage (copie dans un répertoire temporaire frère, puis renommage atomique : une commande interrompue ne laisse jamais un fichier
  partiel ou non vérifié dans l'ensemble publié). Les originaux ne sont pas la preuve : on ne publie jamais depuis eux.

Sont exclus de l'ensemble publié (et rendent le verdict rouge) : un fichier dont le contenu ou le nom contient la clé, un fichier ou un
répertoire illisible (parcours en erreur : l'analyse est INCOMPLÈTE, jamais « clean »), tout lien symbolique, tout objet non régulier.
Les fichiers contaminés sont aussi déplacés en quarantaine hors des répertoires sources. Rien n'est jamais imprimé ni écrit avec la valeur :
les chemins du rapport sont EXPURGÉS (toute forme de la clé remplacée par ``[REDACTED]``) et accompagnés d'un identifiant opaque ; les erreurs
sont des noms de classes.

Codes de sortie : 0 = tout vérifié et publié ; 1 = fuite trouvée (fichiers contaminés exclus, le reste publié) ; 2 = analyse impossible ou
incomplète (clé absente ou trop courte, source absente, parcours ou lecture en erreur, quarantaine ou copie impossible). Non nul ⇒ campagne
rouge. ``--assume-no-key`` (réservé à une campagne NON lancée : la clé n'a jamais existé dans l'environnement) ne cherche rien mais applique
les mêmes règles de nature (fichiers réguliers seulement) et la même construction atomique.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MIN_KEY_LENGTH = 8  # en dessous, la recherche d'une sous-chaîne n'a plus de sens (faux positifs) : analyse refusée
CHUNK_SIZE = 1 << 20
REDACTED = "[REDACTED]"


def variants(key: str) -> Dict[str, bytes]:
    """Formes de la clé à chercher (nom -> octets), jamais affichées."""
    raw = key.encode("utf-8")
    forms: Dict[str, bytes] = {"raw": raw}
    quoted = urllib.parse.quote(key, safe="").encode("ascii")
    if quoted != raw:
        forms["url"] = quoted
    escaped = json.dumps(key)[1:-1].encode("ascii")
    if escaped != raw:
        forms["json"] = escaped
    # base64 : la clé peut commencer à 3 alignements ; on ne garde que les groupes de 4 caractères entièrement déterminés par la clé
    for shift in range(3):
        encoded = base64.b64encode(b"\0" * shift + raw + b"\0\0\0")
        first = -(-shift // 3)  # premier groupe de 3 octets entièrement dans la clé
        last = (shift + len(raw)) // 3  # exclu
        piece = encoded[4 * first : 4 * last]
        if len(piece) >= MIN_KEY_LENGTH:
            forms[f"base64+{shift}"] = piece
    return forms


def found_in(data: bytes, forms: Dict[str, bytes]) -> List[str]:
    return sorted(name for name, form in forms.items() if form in data)


def redact(text: str, forms: Dict[str, bytes]) -> str:
    """Texte (chemin, nom) sans AUCUNE forme de la clé ; sur un nom non décodable, un identifiant opaque remplace tout."""
    out = text
    for form in sorted(forms.values(), key=len, reverse=True):
        try:
            out = out.replace(form.decode("utf-8"), REDACTED)
        except UnicodeDecodeError:
            continue
    return out


def opaque(label: str, relative: str) -> str:
    return hashlib.sha256(f"{label}:{relative}".encode("utf-8", "surrogateescape")).hexdigest()[:16]


def name_leak(name: str, forms: Dict[str, bytes]) -> List[str]:
    return found_in(name.encode("utf-8", "surrogateescape"), forms)


def scan_file(path: Path, forms: Dict[str, bytes], chunk_size: int = CHUNK_SIZE) -> List[str]:
    """Noms des formes trouvées dans ``path`` (octets, par blocs avec recouvrement)."""
    overlap = max(len(form) for form in forms.values()) - 1
    found: set = set()
    tail = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            data = tail + block
            found.update(found_in(data, forms))
            tail = data[-overlap:] if overlap else b""
    return sorted(found)


def verify_and_copy(
    path: Path, destination: Path, temporary: Path, forms: Optional[Dict[str, bytes]], chunk_size: int
) -> List[str]:
    """Lit ``path`` UNE fois : vérifie chaque bloc et le copie ; ne renomme vers ``destination`` que si rien n'a été trouvé.

    Retourne les formes trouvées (copie alors abandonnée). Lève ``OSError`` si lecture ou écriture échoue (copie abandonnée)."""
    temporary.parent.mkdir(parents=True, exist_ok=True)
    overlap = (max(len(form) for form in forms.values()) - 1) if forms else 0
    found: set = set()
    tail = b""
    try:
        with path.open("rb") as source, temporary.open("wb") as target:
            while True:
                block = source.read(chunk_size)
                if not block:
                    break
                if forms:
                    data = tail + block
                    found.update(found_in(data, forms))
                    tail = data[-overlap:] if overlap else b""
                target.write(block)
        if found:
            return sorted(found)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
        return []
    finally:
        if temporary.exists():
            temporary.unlink()


def _quarantine(path: Path, anchor: Path, quarantine: Path) -> Optional[str]:
    """Retire ``path`` du répertoire source ; ``None`` si réussi, sinon le nom de la classe d'erreur (jamais de chemin ni de valeur)."""
    try:
        quarantine.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        else:
            shutil.move(
                str(path), str(quarantine / hashlib.sha256(str(path.relative_to(anchor)).encode()).hexdigest()[:24])
            )
        return None
    except OSError as exc:
        try:
            path.unlink()
            return None
        except OSError:
            return type(exc).__name__


class _Scan:
    def __init__(self, key: Optional[str], quarantine: Path, chunk_size: int, stage: Optional[Path]):
        self.forms = variants(key) if key else None
        self.quarantine, self.chunk, self.stage = quarantine, chunk_size, stage
        self.temp = (stage.parent / (stage.name + ".tmp")) if stage else None
        self.code = 0
        self.report: Dict[str, Any] = {
            "scanned_files": 0,
            "scanned_bytes": 0,
            "staged_files": 0,
            "forms_searched": sorted(self.forms) if self.forms else [],
            "leaks": [],
            "unreadable": [],
            "irregular_removed": [],
            "quarantine_failures": [],
        }

    def shown(self, relative: str) -> str:
        return redact(relative, self.forms) if self.forms else relative

    def fail(self, label: str, relative: str, reason: str) -> None:
        self.report["unreadable"].append(
            {"source": label, "id": opaque(label, relative), "path": self.shown(relative), "reason": reason}
        )
        self.code = 2

    def leak(self, label: str, relative: str, forms: List[str], where: str, size: int = 0) -> None:
        self.report["leaks"].append(
            {
                "source": label,
                "id": opaque(label, relative),
                "path": self.shown(relative),
                "forms": forms,
                "where": where,
                "bytes": size,
            }
        )
        if self.code == 0:
            self.code = 1

    def quarantine_entry(self, path: Path, anchor: Path, label: str, relative: str) -> None:
        failure = _quarantine(path, anchor, self.quarantine)
        if failure:
            self.report["quarantine_failures"].append(
                {"source": label, "id": opaque(label, relative), "reason": failure}
            )
            self.code = 2

    def walk(self, label: str, directory: Path, anchor: Path, glob: Optional[str]) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:  # parcours en erreur : jamais « clean » (os.walk l'aurait ignoré en silence)
            relative = str(directory.relative_to(anchor)) if directory != anchor else "."
            self.fail(label, relative, type(exc).__name__)
            if directory != anchor:
                self.quarantine_entry(directory, anchor, label, relative)
            return
        for entry in entries:
            path = Path(entry.path)
            relative = str(path.relative_to(anchor))
            hits = name_leak(entry.name, self.forms) if self.forms else []
            if hits:  # le NOM contient la clé : exclu, jamais copié ni recopié dans une sortie
                self.leak(label, relative, hits, "name")
                self.quarantine_entry(path, anchor, label, relative)
                continue
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                self.fail(label, relative, type(exc).__name__)
                continue
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                self.report["irregular_removed"].append(
                    {"source": label, "id": opaque(label, relative), "path": self.shown(relative)}
                )
                self.quarantine_entry(path, anchor, label, relative)
                continue
            if stat.S_ISDIR(mode):
                self.walk(label, path, anchor, glob)
                continue
            if glob and not _matches(relative, glob):
                continue
            self.file(label, path, anchor, relative)

    def file(self, label: str, path: Path, anchor: Path, relative: str) -> None:
        destination = (self.stage / label / relative) if self.stage else None
        temporary = (self.temp / (opaque(label, relative) + ".part")) if self.temp else None
        try:
            if destination is not None and temporary is not None:
                found = verify_and_copy(path, destination, temporary, self.forms, self.chunk)
            else:
                found = scan_file(path, self.forms, self.chunk) if self.forms else []
            self.report["scanned_files"] += 1
            self.report["scanned_bytes"] += path.stat().st_size
        except OSError as exc:
            self.fail(label, relative, type(exc).__name__)
            self.quarantine_entry(path, anchor, label, relative)
            return
        if found:
            self.leak(label, relative, found, "content", path.stat().st_size)
            self.quarantine_entry(path, anchor, label, relative)
        elif destination is not None:
            self.report["staged_files"] += 1


def _matches(relative: str, glob: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(Path(relative).name, glob)


def scan_tree(
    roots: Sequence[Path],
    key: Optional[str],
    quarantine: Path,
    chunk_size: int = CHUNK_SIZE,
    *,
    stage: Optional[Path] = None,
    labels: Optional[Sequence[str]] = None,
    globs: Optional[Sequence[Optional[str]]] = None,
) -> Tuple[Dict[str, Any], int]:
    """Analyse ``roots`` ; avec ``stage``, construit l'ensemble publiable (``stage/<étiquette>/…``) de fichiers vérifiés et copiés."""
    scan = _Scan(key, quarantine, chunk_size, stage)
    for index, root in enumerate(roots):
        label = labels[index] if labels else f"root{index}"
        glob = globs[index] if globs else None
        if not root.is_dir():
            scan.fail(label, ".", "répertoire absent")
            continue
        if stage is not None:
            (stage / label).mkdir(parents=True, exist_ok=True)
        scan.walk(label, root, root, glob)
    if scan.report["quarantine_failures"]:
        scan.code = 2
    return scan.report, scan.code


def _parse_source(spec: str) -> Tuple[str, Path, Optional[str]]:
    label, _, rest = spec.partition("=")
    path, _, glob = rest.partition("::")
    if not label or not path or not label.replace("-", "").replace("_", "").isalnum():
        raise argparse.ArgumentTypeError("--source LABEL=DIR[::GLOB]")
    return label, Path(path), (glob or None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", help="NOM de la variable d'environnement qui porte la clé (jamais sa valeur)")
    parser.add_argument(
        "--assume-no-key",
        action="store_true",
        help="campagne NON lancée : la clé n'a jamais existé ; mêmes règles de nature",
    )
    parser.add_argument("--quarantine", type=Path, required=True, help="répertoire HORS des sources")
    parser.add_argument(
        "--stage", type=Path, required=True, help="ensemble publiable (créé ; seuls des fichiers vérifiés y entrent)"
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="rapport JSON (écrit après l'analyse, hors de l'ensemble publiable)"
    )
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--source", action="append", required=True, type=_parse_source, help="LABEL=DIR[::GLOB]")
    args = parser.parse_args(argv)
    if bool(args.env) == bool(args.assume_no_key):
        print("w5_leak_scan: exactement un de --env ou --assume-no-key est requis", file=sys.stderr)
        return 2
    key: Optional[str] = None
    if args.env:
        key = os.environ.get(args.env, "")
        if len(key) < MIN_KEY_LENGTH:
            print(
                f"w5_leak_scan: ${args.env} absente ou trop courte : analyse impossible (campagne rouge)",
                file=sys.stderr,
            )
            return 2
    quarantine, stage = args.quarantine.resolve(), args.stage.resolve()
    if stage == quarantine or args.report.resolve().is_relative_to(stage) or quarantine.is_relative_to(stage):
        print("w5_leak_scan: le rapport et la quarantaine doivent être hors de l'ensemble publiable", file=sys.stderr)
        return 2
    for _label, root, _glob in args.source:
        resolved = root.resolve()
        paths = (quarantine, stage)
        if any(p == resolved or resolved in p.parents or p in resolved.parents for p in paths):
            print(
                "w5_leak_scan: quarantaine et ensemble publiable doivent être hors des sources (et ne pas les contenir)",
                file=sys.stderr,
            )
            return 2
    labels = [label for label, _p, _g in args.source]
    report, code = scan_tree(
        [path for _l, path, _g in args.source],
        key,
        quarantine,
        args.chunk_size,
        stage=stage,
        labels=labels,
        globs=[glob for _l, _p, glob in args.source],
    )
    report["verdict"] = {0: "clean", 1: "leak", 2: "incomplete"}[code]
    report["key_proof"] = "env" if key else "no-key-assumed"
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if key and (
        found_in(text.encode("utf-8"), variants(key))
    ):  # ceinture : le rapport lui-même ne doit contenir aucune forme
        print("w5_leak_scan: le rapport contenait une forme de la clé : non écrit (campagne rouge)", file=sys.stderr)
        return 2
    args.report.write_text(text, encoding="utf-8")
    summary = {k: report[k] for k in ("verdict", "scanned_files", "scanned_bytes", "staged_files")}
    summary["leak_ids"] = [item["id"] for item in report["leaks"]]
    print(json.dumps(summary, ensure_ascii=False))
    if code == 1:
        print(
            "::error::la clé du fournisseur figurait dans un fichier ou un nom : exclu de l'ensemble publiable, campagne invalidée"
        )
    if code == 2:
        print("::error::analyse de fuite incomplète : campagne invalidée (seuls des fichiers vérifiés sont publiés)")
    return code


if __name__ == "__main__":
    sys.exit(main())
