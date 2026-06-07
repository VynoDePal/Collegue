"""Générateur de SPEC.md (P1, #351) : problématique → contrat structuré.

À partir d'une problématique en une phrase, produit un :class:`Spec` (objectifs,
périmètre, contraintes, **hypothèses**, **critères d'acceptation testables**) via un
appel LLM routé sur le rôle **PLANNER** (C1/C2), puis le rend en Markdown et le
persiste dans ``Project.spec`` (state store C6).

Décision epic #351 : en cas d'ambiguïté, le planificateur **ne pose pas de
question** — il consigne ses hypothèses dans ``assumptions`` et avance ; l'humain
valide le plan complet plus tard (P5).

Frontière de confiance : la sortie LLM n'est **pas** fiable. Le rendu Markdown
**neutralise** chaque champ (mise sur une seule ligne) pour qu'un titre/résumé
malveillant ne puisse pas injecter de fausses sections ou des critères « déjà
cochés » dans le document soumis à validation humaine. ``generate_spec`` **rejette**
un SPEC sans critère d'acceptation (contrat vide = inacceptable).

Module **isolé** : non câblé au runtime (le pilote Phase 3 fournira le ``ctx``).
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field, ValidationError

from collegue.core.llm import LLMRole, model_preferences_for_role
from collegue.core.llm.client import sample_with_timeout

# Statuts de projet partagés avec P5 (transition planned → approved au gate humain).
PROJECT_STATUS_PLANNED = "planned"

SPEC_SYSTEM_PROMPT = """Tu es un planificateur logiciel senior. À partir d'une problématique \
(souvent une seule phrase), tu produis un contrat de projet clair et actionnable.

Réponds UNIQUEMENT par un objet JSON valide, sans texte autour, avec ces clés :
- "title": titre court du projet (string)
- "summary": résumé en 1-3 phrases (string)
- "objectives": objectifs (liste de strings)
- "scope": périmètre — ce qui est inclus/exclu (string)
- "constraints": contraintes techniques/produit (liste de strings)
- "assumptions": hypothèses (liste de strings)
- "acceptance_criteria": critères d'acceptation TESTABLES, vérifiables objectivement (liste de strings)

Règles :
- Les critères d'acceptation doivent être testables (un humain ou un test peut trancher vrai/faux).
- Au moins un critère d'acceptation est obligatoire.
- Si la problématique est ambiguë, NE POSE PAS de question : consigne des hypothèses \
explicites dans "assumptions" et avance.
- Reste concis et concret."""


def _inline(text: Any) -> str:
    """Réduit une valeur à une seule ligne (anti-injection Markdown).

    Écrase tout enchaînement d'espaces/sauts de ligne en une espace : un champ LLM
    ne peut donc pas démarrer une nouvelle ligne, donc pas de fausse section
    (``## ...``) ni de case déjà cochée (``- [x] ...``) dans le SPEC rendu.
    """
    return re.sub(r"\s+", " ", str(text)).strip()


class Spec(BaseModel):
    """Contrat de projet structuré, rendu en SPEC.md."""

    title: str
    summary: str = ""
    objectives: List[str] = Field(default_factory=list)
    scope: str = ""
    constraints: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Rend le SPEC en Markdown. Chaque champ est mis sur une ligne (anti-injection) ;
        toutes les sections sont présentes (les vides portent une note)."""

        def bullets(items: List[str], placeholder: str) -> str:
            cleaned = [_inline(i) for i in (items or []) if str(i).strip()]
            return "\n".join(f"- {i}" for i in cleaned) if cleaned else f"_{placeholder}_"

        def checks(items: List[str], placeholder: str) -> str:
            cleaned = [_inline(i) for i in (items or []) if str(i).strip()]
            return "\n".join(f"- [ ] {i}" for i in cleaned) if cleaned else f"_{placeholder}_"

        return (
            f"# {_inline(self.title)}\n\n"
            f"{_inline(self.summary)}\n\n"
            f"## Objectifs\n{bullets(self.objectives, 'À préciser.')}\n\n"
            f"## Périmètre\n{_inline(self.scope) or '_À préciser._'}\n\n"
            f"## Contraintes\n{bullets(self.constraints, 'Aucune contrainte identifiée.')}\n\n"
            f"## Hypothèses\n{bullets(self.assumptions, 'Aucune hypothèse (problématique non ambiguë).')}\n\n"
            f"## Critères d'acceptation\n{checks(self.acceptance_criteria, 'À préciser.')}\n"
        )


def _json_from_text(text: str) -> Optional[dict]:
    """Extrait le premier objet JSON exploitable d'un texte (fallback structured-output).

    Scan brace-balanced via ``raw_decode`` depuis chaque ``{`` : on retourne le
    PREMIER objet complet qui décode (gère les blocs multiples, les ```json fences
    et la prose qui suit, contrairement à un ``{.*}`` glouton).
    """
    if not text:
        return None
    cleaned = text.strip()
    try:
        whole = json.loads(cleaned)
        if isinstance(whole, dict):
            return whole
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _spec_from_dict(data: dict) -> Spec:
    """Valide un dict en Spec ; convertit ``ValidationError`` en ``ValueError`` (contrat du module)."""
    try:
        return Spec.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"SPEC LLM invalide (champs manquants/typés): {exc}") from exc


def _extract_spec(result: Any) -> Spec:
    """Récupère un Spec depuis un SamplingResult : structuré si dispo, sinon JSON du texte."""
    candidate = getattr(result, "result", None)
    if isinstance(candidate, Spec):
        return candidate
    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()
    if isinstance(candidate, str):
        candidate = _json_from_text(candidate)
    if isinstance(candidate, dict):
        return _spec_from_dict(candidate)
    data = _json_from_text(getattr(result, "text", "") or "")
    if data is None:
        raise ValueError("Le planificateur n'a pas retourné de SPEC exploitable (ni structuré ni JSON).")
    return _spec_from_dict(data)


def _build_user_prompt(problem: str, context: Optional[str]) -> str:
    prompt = f"Problématique :\n{problem.strip()}\n"
    if context:
        prompt += f"\nContexte additionnel :\n{context.strip()}\n"
    prompt += "\nProduis le contrat JSON décrit par tes instructions système."
    return prompt


async def generate_spec(
    problem: str,
    ctx: Any,
    *,
    context: Optional[str] = None,
    settings_obj: Optional[object] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> Spec:
    """Génère un :class:`Spec` depuis une problématique via le rôle PLANNER.

    ``ctx`` est le contexte de sampling (FastMCP) — le pilote Phase 3 le fournira ;
    les tests injectent un double. Lève ``ValueError`` si la réponse est inexploitable
    ou si le SPEC ne contient **aucun critère d'acceptation** (contrat vide).
    """
    sample_kwargs = {
        "messages": _build_user_prompt(problem, context),
        "system_prompt": SPEC_SYSTEM_PROMPT,
        "result_type": Spec,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    prefs = model_preferences_for_role(LLMRole.PLANNER, settings_obj)
    if prefs:
        sample_kwargs["model_preferences"] = prefs

    result = await sample_with_timeout(ctx, settings_obj=settings_obj, **sample_kwargs)
    spec = _extract_spec(result)
    if not [a for a in spec.acceptance_criteria if str(a).strip()]:
        raise ValueError("SPEC invalide : aucun critère d'acceptation testable (contrat vide).")
    return spec


def persist_spec(
    manager: Any,
    name: str,
    spec: Spec,
    *,
    deadline: Any = None,
    status: str = PROJECT_STATUS_PLANNED,
) -> int:
    """Persiste le SPEC (rendu Markdown) dans ``Project.spec`` ; retourne le project_id."""
    return manager.create_project(name=name, spec=spec.to_markdown(), deadline=deadline, phase="1", status=status)
