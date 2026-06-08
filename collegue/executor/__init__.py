"""Exécuteur d'une issue de bout en bout (Phase 2, epic #362).

Automatise **une** issue : workspace en sandbox → agent codeur (OpenHands) →
tests + revue experte → Pull Request gatée par la CI. E1 pose le contrat de
l'agent codeur (:class:`CodeAgent`) et son adaptateur OpenHands.

Module **isolé** : non importé par ``app.py``. Le pilote (Phase 3) câblera
l'exécuteur sur le graphe de tâches. Import paresseux d'``OpenHandsAgent`` : il
dépend de la couche LLM, mais surtout on garde ``collegue.executor`` importable
sans rien tirer d'OpenHands.
"""

from collegue.executor.agent import AgentResult, CodeAgent, FakeCodeAgent, IssueSpec
from collegue.executor.openhands_agent import OpenHandsAgent

__all__ = [
    "IssueSpec",
    "AgentResult",
    "CodeAgent",
    "FakeCodeAgent",
    "OpenHandsAgent",
]
