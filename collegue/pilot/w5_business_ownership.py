"""Propriété DURABLE des ressources de la campagne W5 : ce que le nettoyage a le droit de fermer, supprimer ou adopter.

Un préfixe de branche, un marqueur de PR, la base commune ou l'ascendance d'un commit sont PUBLICS : n'importe quel contributeur peut
les reproduire. Ils ne prouvent donc jamais qu'une ressource appartient à CETTE campagne. Une ressource n'est nettoyable que si elle est
consignée dans un état durable de CE projet/cette campagne — puis recoupée avec GitHub (identité, tête, base) :

* **état du produit** : preuves de livraison persistées par ``open_pr`` (journal de décisions du projet : PR et têtes réellement ouvertes
  par le produit pour ce projet, BUILD et IMPROVE) et cycles de fusion des tâches ;
* **registre d'appartenance de la campagne** (``<manifeste>.owned.jsonl``, append-only, écrit AVANT chaque création distante) : branche
  et PR de la SPEC, PR d'amélioration et d'incident, PR de revert, fusions — chaque ligne porte l'identité (dépôt, base, projet, plan).

Règles : une INTENTION seule n'est pas une preuve de création (elle n'est écrite que si l'ABSENCE de la ressource a été établie par un
404 confirmé) ; une ressource déjà présente, empruntée ou ambiguë est conservée et signalée, jamais supprimée ; aucune ressource n'est
adoptée par ressemblance de nom, de numéro ou de texte.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

LEDGER_SUFFIX = ".owned.jsonl"
#: Familles de branches que la campagne crée elle-même (jamais ``main``, jamais le socle, jamais ``collegue-business-claims/``).
BRANCH_FAMILIES = ("collegue/issue-", "collegue/improve-", "collegue/revert-", "collegue-spec/")
_PROOF = re.compile(r"delivery-proof:v1:(?P<repo>[^#\s]+)#(?P<pr>[0-9]+)@(?P<head>[0-9a-f]{40}):")


class OwnershipError(RuntimeError):
    """L'état d'appartenance est illisible ou incohérent : aucune ressource n'est nettoyée sur cette base."""


def ledger_file(manifest_path: str) -> str:
    return str(manifest_path) + LEDGER_SUFFIX


def identity_of(
    owner: str, repo: str, base: str, *, project_id: Optional[int] = None, plan_hash: Optional[str] = None
) -> Dict[str, Any]:
    return {"repo": f"{owner}/{repo}".lower(), "base": base, "project_id": project_id, "plan_hash": plan_hash}


def append_event(manifest_path: str, identity: Mapping[str, Any], event: str, **fields: Any) -> None:
    """Ajoute UNE ligne durable (append-only, fsync). À appeler AVANT l'effet distant qu'elle annonce."""
    path = Path(ledger_file(manifest_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"v": 1, "event": event, **dict(identity), **fields}, sort_keys=True, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_events(
    manifest_path: str, *, repo: str, base: Optional[str] = None, project_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Événements de ce dépôt (et de cette base / ce projet si fournis). Ligne illisible ⇒ ``OwnershipError`` (fail-closed)."""
    path = Path(ledger_file(manifest_path))
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OwnershipError(f"registre d'appartenance illisible ({type(exc).__name__})") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise OwnershipError(f"registre d'appartenance corrompu (ligne {number})") from exc
        if not isinstance(entry, dict) or entry.get("v") != 1 or not isinstance(entry.get("event"), str):
            raise OwnershipError(f"registre d'appartenance malformé (ligne {number})")
        if str(entry.get("repo", "")).lower() != repo.lower():
            continue
        if base is not None and entry.get("base") != base:
            continue
        if project_id is not None and entry.get("project_id") not in (None, project_id):
            continue
        events.append(entry)
    return events


def proof_pull_requests(manager: Any, project_id: Optional[int], owner: str, repo: str) -> Dict[int, Dict[str, str]]:
    """PR réellement ouvertes par le produit pour CE projet : ``{numéro: {tête: phase}}`` d'après les preuves de livraison persistées
    (relues et validées par ``load_delivery_proof`` : identités projet/dépôt/PR/tête, ``proof_id`` recalculé)."""
    if manager is None or not project_id:
        return {}
    from collegue.executor.delivery_proof import DECISION_PREFIX, DeliveryProofError, load_delivery_proof

    found: Dict[int, Dict[str, str]] = {}
    try:
        entries = manager.get_decision_journal(int(project_id), f"{DECISION_PREFIX}{owner}/{repo}#")
    except Exception as exc:  # noqa: BLE001
        raise OwnershipError(f"journal de décisions illisible ({type(exc).__name__})") from exc
    for entry in entries:
        match = _PROOF.match(str(getattr(entry, "summary", "") or ""))
        if match is None or match.group("repo") != f"{owner}/{repo}":
            continue
        number, head = int(match.group("pr")), match.group("head")
        try:
            proof = load_delivery_proof(
                manager, int(project_id), owner=owner, repo=repo, pr_number=number, head_sha=head
            )
        except DeliveryProofError as exc:
            raise OwnershipError(f"preuve de livraison de la PR #{number} invalide : {exc}") from exc
        found.setdefault(number, {})[head] = proof.phase
    return found


def owned_pull_requests(
    manager: Any, project_id: Optional[int], owner: str, repo: str, events: List[Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """``{numéro de PR: {"heads": {sha, …}, "sources": {…}}}`` des PR de CETTE campagne (preuves du produit + registre)."""
    owned: Dict[int, Dict[str, Any]] = {}

    def add(number: Any, head: Any, source: str) -> None:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return
        slot = owned.setdefault(number, {"heads": set(), "sources": set()})
        slot["sources"].add(source)
        if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head):
            slot["heads"].add(head)

    for number, heads in proof_pull_requests(manager, project_id, owner, repo).items():
        for head, phase in heads.items():
            add(number, head, f"preuve:{phase}")
    for entry in events:
        if entry["event"] in {"spec_pr", "improve_pr", "incident_pr", "revert_pr"}:
            add(entry.get("pr_number"), entry.get("head_sha"), f"registre:{entry['event']}")
    return owned


def merge_commits(
    manager: Any,
    project_id: Optional[int],
    clients: Any,
    owner: str,
    repo: str,
    owned: Mapping[int, Mapping[str, Any]],
    events: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Commits de fusion EXPLIQUÉS par une attribution durable à la campagne : ``{sha: origine}``.

    Sources : cycles de fusion des tâches (BUILD), fusions consignées au registre (SPEC, incident, revert, amélioration), puis chaque
    PR possédée relue sur GitHub (fusionnée, tête connue, ``merge_commit_sha``)."""
    explained: Dict[str, str] = {}
    for entry in events:
        sha = entry.get("merge_sha")
        if (
            entry["event"] in {"spec_merged", "improve_pr", "incident_pr", "revert_pr"}
            and isinstance(sha, str)
            and len(sha) == 40
        ):
            explained[sha.lower()] = f"registre:{entry['event']}"
    if manager is not None and project_id:
        try:
            for row in manager.list_task_merges(int(project_id)):
                sha = getattr(row, "merge_sha", None)
                if isinstance(sha, str) and len(sha) == 40:
                    explained[sha.lower()] = f"cycle de fusion de la tâche {getattr(row, 'task_id', '?')}"
        except Exception as exc:  # noqa: BLE001
            raise OwnershipError(f"cycles de fusion illisibles ({type(exc).__name__})") from exc
    for number, info in owned.items():
        live = clients.prs.get_pr(owner, repo, int(number))
        sha = getattr(live, "merge_commit_sha", None)
        if getattr(live, "merged", False) and str(getattr(live, "head_sha", "")).lower() in info["heads"] and sha:
            explained.setdefault(str(sha).lower(), f"PR #{number} ({', '.join(sorted(info['sources']))})")
    return explained
