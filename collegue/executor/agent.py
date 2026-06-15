"""Contrat de l'agent codeur (E1, epic #362, brief §7 Phase 2).

Frontière d'intégration de l'« exécuteur d'une issue » : un protocole
:class:`CodeAgent` **indépendant** de la solution concrète (OpenHands ou autre),
plus un :class:`FakeCodeAgent` déterministe pour la CI/les tests des enfants
suivants (E2→E5). L'adaptateur OpenHands réel vit dans
``collegue.executor.openhands_agent`` (exécution réelle derrière le marqueur
``integration``).

Découpage des responsabilités : l'agent **mute** le workspace (écrit/modifie des
fichiers) ; la **capture autoritative du diff** (``git diff``) est faite par
l'exécuteur (E2), pas ici. ``AgentResult.files_changed`` reste un *best-effort*
auto-déclaré par l'agent.

Module **isolé** : non câblé au runtime (le pilote Phase 3 câblera l'exécuteur).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Tuple, runtime_checkable

from collegue.textnorm import inline


@dataclass(frozen=True)
class IssueSpec:
    """Issue normalisée passée à l'agent codeur.

    ``number``/``title`` sont obligatoires ; ``body`` et ``acceptance_criteria``
    sont optionnels. Le texte est **non fiable** (rédigé par un humain ou un LLM) :
    :meth:`to_prompt` le passe par ``inline`` pour neutraliser l'injection Markdown
    (pas de fausse section ``## ...`` ni de case ``- [x] ...`` qui détournerait la
    consigne).
    """

    number: int
    title: str
    body: str = ""
    acceptance_criteria: Tuple[str, ...] = ()
    # Contexte inter-tâches (dépendances déjà construites, état du dépôt, conventions)
    # injecté par le pilote pour que l'agent BÂTISSE sur l'existant au lieu de coder
    # depuis une consigne d'une ligne → cohérence inter-tâches. Voir issue #412.
    context: str = ""

    def to_prompt(self) -> str:
        """Construit la consigne (sanitizée) donnée à l'agent.

        Tout champ d'issue est ``inline``-isé : on accepte la perte de structure
        multi-ligne du ``body`` en échange de la garantie qu'aucun contenu d'issue
        ne peut forger une nouvelle ligne/section dans la consigne rendue.
        """
        lines = [f"Issue #{self.number}: {inline(self.title)}"]
        context = inline(self.context)
        if context:
            lines += ["", f"Contexte : {context}"]
        body = inline(self.body)
        if body:
            lines += ["", body]
        criteria = [inline(c) for c in self.acceptance_criteria if inline(c)]
        if criteria:
            lines += ["", "Critères d'acceptation :"]
            lines += [f"- {c}" for c in criteria]
        return "\n".join(lines)


@dataclass
class AgentResult:
    """Résultat d'une passe de l'agent codeur sur une issue.

    ``diff``/``files_changed`` sont auto-déclarés (best-effort) ; la capture
    autoritative du diff revient à l'exécuteur (E2). ``success=False`` signale que
    l'agent n'a pas pu produire de changement exploitable.

    Usage LLM (#441) : auto-déclaré par l'agent (0 = inconnu/non rapporté) — le
    pilote l'ajoute au ledger de coût du run. Sans ce canal, la dépense du coder
    (majoritaire) reste invisible de toute gouvernance (ledger à 0 $ / 0 token
    sur ~7 h de LLM au run FacNor v2).
    """

    success: bool
    diff: str = ""
    files_changed: Tuple[str, ...] = ()
    logs: str = ""
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # #504 : True si le coût rapporté est AUTORITAIRE même nul (ex. abonnement,
    # ``billable: false`` → coût réel 0) ; le pilote ne re-tarife alors PAS au prix
    # de secours #484. Défaut False : un coût 0 = INCONNU (modèle non mappé) → #484.
    cost_authoritative: bool = False

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)


@runtime_checkable
class CodeAgent(Protocol):
    """Protocole d'un agent capable d'implémenter une issue dans un workspace.

    L'agent reçoit le chemin d'un workspace git déjà préparé (E2) et l'issue ; il
    modifie les fichiers en place et renvoie un :class:`AgentResult`. Implémentations :
    :class:`FakeCodeAgent` (CI) et ``OpenHandsAgent`` (réel, derrière ``integration``).
    """

    def implement_issue(self, workspace: str, issue: IssueSpec) -> AgentResult: ...


def _safe_join(workspace: str, relative: str) -> str:
    """Joint ``relative`` à ``workspace`` en refusant toute évasion (``..``, absolu).

    Même esprit que ``sandbox._validate_workspace`` : on ``realpath`` puis on vérifie
    que le résultat reste confiné dans le workspace, pour qu'un chemin hostile ne
    puisse pas écrire hors de la racine.
    """
    root = os.path.realpath(os.path.abspath(workspace))
    target = os.path.realpath(os.path.join(root, relative))
    if target == root:
        raise ValueError(f"chemin vide / répertoire (pas un fichier): {relative!r}")
    if os.path.commonpath([target, root]) != root:
        raise ValueError(f"chemin hors du workspace {root}: {relative}")
    return target


class FakeCodeAgent:
    """:class:`CodeAgent` déterministe pour la CI : écrit un jeu de fichiers fixe.

    Sert de double de test aux enfants E2→E5 (workspace réel modifié → ``git diff``
    non vide). ``succeed=False`` simule un agent qui échoue (aucun fichier écrit).
    """

    def __init__(
        self,
        files: Optional[Mapping[str, str]] = None,
        *,
        succeed: bool = True,
        summary: str = "changement simulé",
    ):
        # Défaut : un fichier marqueur déterministe (suffit à produire un diff).
        self._files = dict(files) if files is not None else {"COLLEGUE_FAKE.txt": "changement simulé\n"}
        self._succeed = succeed
        self._summary = summary

    def implement_issue(self, workspace: str, issue: IssueSpec) -> AgentResult:
        if not self._succeed:
            return AgentResult(success=False, logs="fake agent : échec simulé", summary="échec simulé")
        written = []
        for relative, content in self._files.items():
            path = _safe_join(workspace, relative)
            # _safe_join garantit path != root, donc dirname(path) est non vide et
            # confiné dans le workspace validé (pas de fallback sur l'arg brut).
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            written.append(relative)
        files = tuple(sorted(written))
        return AgentResult(
            success=True,
            files_changed=files,
            summary=self._summary,
            logs=f"fake agent : {len(files)} fichier(s) écrit(s) pour l'issue #{issue.number}",
        )
