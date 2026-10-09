#!/usr/bin/env python3
"""Scanner de confiance : aucun fichier déposé en artefact ne doit contenir la clé du fournisseur (propriété C, vague 5).

Lancé par l'étape de campagne du workflow (la clé n'existe que dans l'environnement de cette étape) APRÈS la campagne et AVANT tout dépôt :

* la VALEUR de la clé est lue dans la variable d'environnement nommée par ``--env`` (jamais dans l'argv, jamais d'un fichier) ; elle n'est
  imprimée ni dans la sortie, ni dans le rapport, ni dans un message d'erreur ;
* TOUS les fichiers des répertoires donnés sont lus **en octets**, binaires compris (registre SQLite, journaux, archives non compressées),
  par blocs avec recouvrement (une clé à cheval sur deux blocs est trouvée) ;
* formes cherchées : brute, encodée pour une URL, échappée JSON, base64 (trois alignements). Une autre transformation n'est pas couverte ;
* un fichier qui contient la clé, un fichier illisible (non vérifiable) et tout lien symbolique (un dépôt d'artefact pourrait le suivre)
  sont MIS EN QUARANTAINE (déplacés hors des répertoires déposés, ou supprimés) : l'artefact déposé ne contient jamais un fichier contaminé
  ou non vérifié ; les autres fichiers (preuves, registre sain) et le nettoyage distant sont conservés ;
* le rapport JSON (chemins relatifs, tailles, formes trouvées) ne contient jamais la valeur.

Codes de sortie : 0 = rien trouvé ; 1 = fuite trouvée (fichiers contaminés mis en quarantaine) ; 2 = analyse impossible ou incomplète (clé absente
ou trop courte, répertoire absent, fichier non lisible, quarantaine impossible). Dans tous les cas non nuls la campagne est rouge.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MIN_KEY_LENGTH = 8  # en dessous, la recherche d'une sous-chaîne n'a plus de sens (faux positifs) : analyse refusée
CHUNK_SIZE = 1 << 20


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
            for name, form in forms.items():
                if form in data:
                    found.add(name)
            tail = data[-overlap:] if overlap else b""
    return sorted(found)


def _quarantine(path: Path, root: Path, quarantine: Path) -> Optional[str]:
    """Retire ``path`` des répertoires déposés ; ``None`` si réussi, sinon le motif d'échec (sans valeur)."""
    try:
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()[:24]
        if path.is_symlink():
            path.unlink()
        else:
            shutil.move(str(path), str(target))
        return None
    except OSError as exc:
        try:
            path.unlink()
            return None
        except OSError:
            return f"{type(exc).__name__}"


def scan_tree(
    roots: Sequence[Path], key: str, quarantine: Path, chunk_size: int = CHUNK_SIZE
) -> Tuple[Dict[str, Any], int]:
    forms = variants(key)
    report: Dict[str, Any] = {
        "scanned_files": 0,
        "scanned_bytes": 0,
        "forms_searched": sorted(forms),
        "leaks": [],
        "unreadable": [],
        "symlinks_removed": [],
        "quarantine_failures": [],
    }
    code = 0
    for root in roots:
        if not root.is_dir():
            report["unreadable"].append({"path": str(root), "reason": "répertoire absent"})
            code = 2
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(current) / name
                if path.is_symlink():
                    report["symlinks_removed"].append(str(path.relative_to(root)))
                    failure = _quarantine(path, root, quarantine)
                    if failure:
                        report["quarantine_failures"].append({"path": str(path.relative_to(root)), "reason": failure})
                        code = 2
            for name in sorted(files):
                path = Path(current) / name
                if path.is_symlink() or not path.exists():
                    continue
                relative = str(path.relative_to(root))
                try:
                    found = scan_file(path, forms, chunk_size)
                    report["scanned_files"] += 1
                    report["scanned_bytes"] += path.stat().st_size
                except OSError as exc:
                    report["unreadable"].append({"path": relative, "reason": type(exc).__name__})
                    failure = _quarantine(path, root, quarantine)
                    if failure:
                        report["quarantine_failures"].append({"path": relative, "reason": failure})
                    code = 2
                    continue
                if found:
                    report["leaks"].append({"path": relative, "forms": found, "bytes": path.stat().st_size})
                    failure = _quarantine(path, root, quarantine)
                    if failure:
                        report["quarantine_failures"].append({"path": relative, "reason": failure})
                        code = 2
                    elif code == 0:
                        code = 1
    if report["quarantine_failures"]:
        code = 2
    return report, code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--env", required=True, help="NOM de la variable d'environnement qui porte la clé (jamais sa valeur)"
    )
    parser.add_argument("--quarantine", type=Path, required=True, help="répertoire HORS des répertoires déposés")
    parser.add_argument(
        "--report", type=Path, required=True, help="rapport JSON (écrit après l'analyse, donc non analysé)"
    )
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args(argv)
    key = os.environ.get(args.env, "")
    if len(key) < MIN_KEY_LENGTH:
        print(
            f"w5_leak_scan: ${args.env} absente ou trop courte : analyse impossible (campagne rouge)", file=sys.stderr
        )
        return 2
    quarantine = args.quarantine.resolve()
    for root in args.roots:
        resolved = root.resolve()
        if quarantine == resolved or resolved in quarantine.parents or quarantine in resolved.parents:
            print(
                "w5_leak_scan: la quarantaine ne doit pas être dans (ni contenir) un répertoire déposé", file=sys.stderr
            )
            return 2
    report, code = scan_tree([r for r in args.roots], key, quarantine, args.chunk_size)
    report["verdict"] = {0: "clean", 1: "leak", 2: "incomplete"}[code]
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {k: report[k] for k in ("verdict", "scanned_files", "scanned_bytes")}
    summary["leaks"] = [item["path"] for item in report["leaks"]]
    print(json.dumps(summary, ensure_ascii=False))
    if code == 1:
        print(
            "::error::la clé du fournisseur figurait dans un fichier de sortie : mis en quarantaine, campagne invalidée"
        )
    if code == 2:
        print("::error::analyse de fuite incomplète : campagne invalidée")
    return code


if __name__ == "__main__":
    sys.exit(main())
