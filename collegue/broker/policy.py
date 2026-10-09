"""Politique statique du courtier W5 : identités officielles, endpoint officiel, droits par rôle.

Les droits d'une session sont fixés par le SERVEUR au moment de l'ouverture (rôle → modèles autorisés) ; ni le rôle, ni le
scope, ni le modèle, ni l'URL soumis dans une requête ne les élargissent. Seules deux identités Gemma 4 existent ; le repli
``gemma-4-26b-a4b-it`` n'est ouvert qu'au rôle ``coder``. Aucun autre fournisseur, endpoint ni modèle n'est généré.
"""

from __future__ import annotations

from typing import Tuple

PRIMARY_MODEL = "gemma-4-31b-it"
FALLBACK_MODEL = "gemma-4-26b-a4b-it"
OFFICIAL_MODELS = (PRIMARY_MODEL, FALLBACK_MODEL)

# Endpoint Google OFFICIEL : fixe, non configurable. Le prix 0 des deux identités n'est attesté que pour lui.
GOOGLE_API_HOST = "generativelanguage.googleapis.com"
GOOGLE_API_BASE = f"https://{GOOGLE_API_HOST}/v1beta"

ROLES = ("coder", "qa", "reviewer", "planner", "default")
FALLBACK_ROLES = ("coder",)

# Plafonds de requête (octets du corps JSON du client) et de sortie par appel.
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 32
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS_CEILING = 8192


def models_for_role(role: str) -> Tuple[str, ...]:
    """Modèles qu'un rôle peut obtenir : la principale pour tous, le repli 26B pour le seul codeur."""
    name = str(role).strip().lower()
    if name not in ROLES:
        raise ValueError(f"rôle inconnu pour le courtier : {role!r}")
    return OFFICIAL_MODELS if name in FALLBACK_ROLES else (PRIMARY_MODEL,)


def canonical_model(name: str) -> str:
    """Nom nu d'une identité officielle (préfixes ``models/``, ``gemini/``, ``openai/`` du client retirés), sinon ``ValueError``.

    Le préfixe ``openai/`` est celui du client LiteLLM compatible Chat Completions : il ne change PAS la destination
    sémantique (toujours Google).
    """
    text = str(name or "").strip()
    for prefix in ("models/", "gemini/", "openai/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    if text not in OFFICIAL_MODELS:
        raise ValueError(f"modèle non autorisé : {name!r} (identités officielles : {', '.join(OFFICIAL_MODELS)})")
    return text
