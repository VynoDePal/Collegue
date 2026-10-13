"""Doublure des SERVICES EXTERNES derrière le VRAI CLI de la paire (``scripts/w5_json_pair.py``), pour les tests du workflow (propriété C).

Exécutable comme ``python tests/w5_json_pair_cli_harness.py <commande> --root … --campaign-id …`` : le shim ``python`` des tests de workflow
redirige ``python scripts/w5_json_pair.py …`` ici. Le CLI est chargé TEL QUEL (même fichier, même ``main``, même ``argparse``, même
environnement réel du processus) ; seules deux fabriques de services externes sont remplacées :

* ``_clients`` (GitHub de la fixture) par un faux dépôt qui enregistre les créations de références ;
* ``_google_upstream`` par le VRAI ``GoogleUpstream`` (hôte, chemins, en-tête de clé, plafond de réponse) dont seule la couche réseau est
  ``httpx.MockTransport`` ; les réponses viennent du faux Google de B (``w5_json_pair_support.Wire``), qui journalise chaque requête reçue.

Spécification : fichier JSON désigné par ``W5_C_HARNESS`` ; aucun secret n'y figure. Ce module n'est jamais importé par le produit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
from w5_json_pair_support import Wire  # noqa: E402

from collegue.broker.upstream import GoogleUpstream  # noqa: E402


class _Branches:
    def __init__(self, spec: dict):
        self.spec = spec
        self.state_file = Path(spec["claims_file"])

    def _existing(self) -> set:
        found = set(self.spec.get("existing", []))
        if self.state_file.exists():
            found |= {line for line in self.state_file.read_text().splitlines() if line}
        return found

    def create_branch(self, owner, repo, ref, from_branch="main"):
        if self.spec.get("create_error"):
            raise RuntimeError("GitHub indisponible")
        if ref in self._existing():
            raise RuntimeError("Reference already exists")
        with self.state_file.open("a") as handle:
            handle.write(ref + "\n")

    def get_branch_sha(self, owner, repo, ref):
        if self.spec.get("read_error"):
            raise RuntimeError("GitHub indisponible")
        if ref in self._existing():
            return "b" * 40
        error = RuntimeError("Not Found")
        error.status_code = 404
        raise error


class _Clients:
    def __init__(self, spec: dict):
        self.branches = _Branches(spec)


def _load_cli():
    spec = importlib.util.spec_from_file_location("w5_json_pair_cli_real", REPO / "scripts" / "w5_json_pair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reflect_key(root: Path, campaign_id: str, key: str) -> None:
    """Simule un REGISTRE qui a reflété la clé (octets du fichier SQLite) : effet de bord de la doublure, pas du CLI."""
    database = root / "home" / f"{campaign_id}.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "update broker_attempts set error_detail = ? where id = (select min(id) from broker_attempts)", (key,)
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    spec = json.loads(Path(os.environ["W5_C_HARNESS"]).read_text(encoding="utf-8"))
    cli = _load_cli()
    wire = Wire(Path(spec["wire_log"]), spec["script"])
    cli._clients = lambda token: _Clients(spec)
    cli._google_upstream = lambda key: GoogleUpstream(
        lambda: key, timeout=5.0, transport=httpx.MockTransport(wire.handler)
    )
    command = sys.argv[1]
    with Path(spec["call_log"]).open("a") as handle:
        handle.write(
            f"{command} LLM_API_KEY={'present' if os.environ.get('LLM_API_KEY') else 'absent'}"
            f" GITHUB_TOKEN={'present' if os.environ.get('GITHUB_TOKEN') else 'absent'}\n"
        )
    code = cli.main(sys.argv[1:])
    if command == "run" and spec.get("reflect_key_in_registry"):
        args = sys.argv[1:]
        root = Path(args[args.index("--root") + 1])
        campaign = args[args.index("--campaign-id") + 1]
        _reflect_key(root, campaign, os.environ["LLM_API_KEY"])
    return code


if __name__ == "__main__":
    sys.exit(main())
