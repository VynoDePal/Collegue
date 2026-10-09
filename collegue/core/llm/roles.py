"""Rôles LLM et résolution de la DESTINATION effective (fournisseur, modèle, endpoint, authentification) par rôle.

Le brief « moteur de dev autonome » (§5) veut des modèles par rôle : un codeur fort, un QA/triage économique, un
planner, etc. Avant la vague 4, seuls ``(provider, model)`` se résolvaient par rôle ; l'endpoint et la clé restaient
ceux du fournisseur GLOBAL : un planner configuré ``openai/gpt-5.4`` partait donc vers l'endpoint Google avec la clé
Gemini. Ce module résout désormais ENSEMBLE ce qui détermine où part un appel et avec quelle identité
(:class:`LLMRoute`), et c'est cette résolution unique que consomment le ``ctx`` offline, le handler de sampling
serveur et le worker OpenHands.

Règles (toute violation lève :class:`LLMRoutingError` AVANT toute émission, message sans secret) :

* fournisseur et modèle : ``LLM_PROVIDER_<ROLE>`` / ``LLM_MODEL_<ROLE>`` sinon le couple global. Un rôle dont le
  fournisseur diffère du global DOIT nommer son modèle (jamais un modèle hérité d'un autre fournisseur) ;
* contradictions refusées : préfixe ``autre-fournisseur/`` dans le modèle, famille de modèle d'un autre fournisseur
  hébergé (``gemini-*`` chez OpenAI, ``gpt-*`` chez Gemini), endpoint hébergé d'un autre fournisseur ;
* clé : ``LLM_API_KEY_<ROLE>``, sinon la clé GLOBALE — uniquement si le fournisseur du rôle est le fournisseur global.
  Un fournisseur local accepte volontairement l'absence de clé ; tout autre cas sans clé est refusé ;
* endpoint : ``LLM_BASE_URL_<ROLE>``, sinon celui du fournisseur global (même fournisseur), sinon le défaut du
  fournisseur ;
* abonnement (ChatGPT/Codex) : explicite (``CODER_SUBSCRIPTION`` pour le codeur, ``LLM_AUTH_<ROLE>=subscription``
  pour tout rôle), réservé au fournisseur ``openai`` et à un modèle OpenAI ; jamais déduit du nom du modèle ;
* catalogue fermé : ``gemini``, ``openai`` et les fournisseurs locaux OpenAI-compatibles ; tout autre est refusé.

Sans configuration par rôle, un rôle résout exactement la destination du fournisseur global : aucun comportement
existant et cohérent ne change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_BASE_URL = "https://api.openai.com/v1"
HOSTED_PROVIDERS = ("gemini", "openai")
LOCAL_PROVIDERS = ("lmstudio", "ollama", "unsloth")
SUPPORTED_PROVIDERS = HOSTED_PROVIDERS + LOCAL_PROVIDERS
LOCAL_DEFAULT_BASE_URLS = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "unsloth": "http://localhost:8888/v1",
}
# Destinations HÉBERGÉES connues (l'hôte fait foi).
HOSTED_ENDPOINT_HOSTS = {"generativelanguage.googleapis.com": "gemini", "api.openai.com": "openai"}

AUTH_API_KEY = "api_key"
AUTH_NONE = "none"  # fournisseur local, aucune clé
AUTH_SUBSCRIPTION = "subscription"
AUTH_METHODS = (AUTH_API_KEY, AUTH_NONE, AUTH_SUBSCRIPTION)

SOURCE_ROLE = "role"
SOURCE_GLOBAL = "global"
SOURCE_NONE = "none"
SOURCE_SUBSCRIPTION = "subscription"

# Marqueur de rôle porté par les ``model_preferences`` (un nom de hint, donc SÉRIALISABLE jusqu'au handler).
ROUTE_HINT_PREFIX = "collegue-route:"

# Familles de noms de modèles propres à un fournisseur HÉBERGÉ (jamais valables chez l'autre).
_OPENAI_FAMILY = re.compile(r"^(gpt-|chatgpt|o[0-9]|codex|text-embedding|davinci)")
_GEMINI_FAMILY = re.compile(r"^(gemini|gemma|models/)")


class LLMRoutingError(ValueError):
    """Routage impossible ou contradictoire : refusé avant toute émission (le message ne contient jamais de secret)."""


class LLMMissingCredentialError(LLMRoutingError):
    """La route est cohérente mais le rôle n'a AUCUNE clé de son fournisseur : refusée à l'appel, avant émission.

    Distincte d'une contradiction (fournisseur/modèle/endpoint) : au démarrage du serveur, un rôle sans clé n'empêche pas
    les rôles indépendamment configurés de servir, alors qu'une contradiction refuse le démarrage.
    """


class LLMRole(str, Enum):
    """Rôle fonctionnel d'un appel LLM, qui détermine le modèle utilisé."""

    CODER = "coder"
    QA = "qa"
    REVIEWER = "reviewer"
    PLANNER = "planner"
    DEFAULT = "default"


@dataclass(frozen=True)
class LLMRoute:
    """Destination effective d'un appel : fournisseur, modèle canonique, endpoint, authentification, provenance.

    ``model`` est le nom NU (sans préfixe de fournisseur). Le credential n'est jamais dans ``repr``/``str`` ni dans
    ``describe()`` ; seule son empreinte (SHA-256 tronqué, non réversible en pratique) et sa provenance le désignent.
    """

    role: str
    provider: str
    model: str
    endpoint: Optional[str]  # URL de base effective ; ``None`` = abonnement (backend du SDK)
    explicit_endpoint: bool  # vrai si l'opérateur l'a nommé (par rôle ou global), faux si défaut du fournisseur
    auth: str
    credential_source: str
    credential_fingerprint: str = ""
    _credential: Optional[str] = field(default=None, repr=False, compare=False)

    def credential(self) -> Optional[str]:
        """Valeur de la clé (à ne transmettre qu'au transport) ; ``None`` sans clé."""
        return self._credential

    def transport_key(self) -> str:
        """Valeur ``api_key`` à donner au client HTTP ÉMETTEUR, ou refus si la route n'a pas de credential.

        ``api_key`` sans credential (route obtenue avec ``require_credential=False``) n'est JAMAIS transformée en clé
        factice ``local`` : seule une route ``none`` (fournisseur local choisi sans clé) reçoit la valeur fictive explicite.
        """
        if self._credential:
            return self._credential
        if self.auth == AUTH_NONE:
            return "local"
        raise LLMMissingCredentialError(
            f"rôle {self.role} : authentification {self.auth} sans credential — émission refusée"
        )

    @property
    def uses_subscription(self) -> bool:
        return self.auth == AUTH_SUBSCRIPTION

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    @property
    def hosted_family(self) -> Optional[str]:
        """Famille hébergée (``gemini``/``openai``) de la destination réelle, sinon ``None``."""
        if self.uses_subscription:
            return "openai"
        host = (urlparse(str(self.endpoint or "")).hostname or "").lower()
        return HOSTED_ENDPOINT_HOSTS.get(host)

    def litellm_model(self) -> str:
        """Nom du modèle pour LiteLLM/OpenHands : préfixe porté par le FOURNISSEUR du rôle (jamais deviné)."""
        if self.uses_subscription:
            return self.model
        return f"gemini/{self.model}" if self.provider == "gemini" else f"openai/{self.model}"

    def worker_base_url(self) -> Optional[str]:
        """``base_url`` à transmettre au SDK du worker, ou ``None`` (défaut du fournisseur pour LiteLLM)."""
        if self.uses_subscription:
            return None
        if self.provider == "gemini":
            if self.explicit_endpoint:
                raise LLMRoutingError(
                    f"rôle {self.role} : un endpoint personnalisé pour le fournisseur gemini n'est pas supporté par le "
                    "worker (LiteLLM route 'gemini/…' vers l'API Google) — retirer LLM_BASE_URL_<ROLE> (ou LLM_BASE_URL global hérité)"
                )
            return None
        if self.provider == "openai" and not self.explicit_endpoint:
            return None
        return self.endpoint

    def describe(self) -> dict:
        """Vue sans secret (journaux, rapports, préflight)."""
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "auth": self.auth,
            "credential_source": self.credential_source,
            "credential_present": bool(self._credential),
            "credential_fingerprint": self.credential_fingerprint,
        }

    def cache_key(self) -> Tuple[str, str, str, str]:
        return (self.provider, str(self.endpoint), self.auth, self.credential_fingerprint)

    def __str__(self) -> str:  # jamais de secret, même via f"{route}"
        return f"{self.role}:{self.provider}/{self.model}@{self.endpoint} ({self.auth}, clé {self.credential_source})"


def _norm_role(role: LLMRole | str) -> str:
    return role.value if isinstance(role, LLMRole) else str(role).lower()


def _text(value: Any) -> str:
    """Valeur de réglage en texte nu : ``SecretStr`` déballé, ``None``/vide → ``""``. Ne lève jamais."""
    if value is None:
        return ""
    reveal = getattr(value, "get_secret_value", None)
    if callable(reveal):
        value = reveal()
    return str(value).strip()


def _setting(settings_obj: object, name: str) -> str:
    return _text(getattr(settings_obj, name, None))


def _fingerprint(secret: Optional[str]) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12] if secret else ""


def _provider(value: str, *, where: str) -> str:
    provider = (value or "").strip().lower()
    return provider or ""


def canonical_model(provider: str, model: str, *, where: str = "modèle") -> str:
    """Nom NU du modèle ; refuse un préfixe/une famille d'un AUTRE fournisseur."""
    name = (model or "").strip()
    if not name:
        raise LLMRoutingError(f"{where} : modèle absent")
    head, sep, rest = name.partition("/")
    head_l = head.lower()
    if sep:
        if head_l == provider and rest:
            name = rest
        elif head_l == "models" and provider == "gemini" and rest:
            name = rest
        elif provider in HOSTED_PROVIDERS and head_l in SUPPORTED_PROVIDERS and head_l != provider:
            raise LLMRoutingError(
                f"{where} : le modèle {name!r} porte le préfixe du fournisseur {head_l!r} alors que le "
                f"fournisseur du rôle est {provider!r} (contradiction refusée, aucune conversion silencieuse)"
            )
    lowered = name.lower()
    if provider == "openai" and _GEMINI_FAMILY.match(lowered):
        raise LLMRoutingError(
            f"{where} : le modèle {name!r} est un modèle Gemini/Gemma, pas OpenAI (fournisseur openai)"
        )
    if provider == "gemini" and _OPENAI_FAMILY.match(lowered):
        raise LLMRoutingError(f"{where} : le modèle {name!r} est un modèle OpenAI, pas Gemini (fournisseur gemini)")
    return name


def _check_endpoint(provider: str, url: str, *, where: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMRoutingError(f"{where} : endpoint invalide (schéma http/https et hôte requis)")
    if parsed.username or parsed.password:
        raise LLMRoutingError(f"{where} : l'endpoint ne doit contenir aucun identifiant (utiliser la clé du rôle)")
    family = HOSTED_ENDPOINT_HOSTS.get(parsed.hostname.lower())
    if family is not None and family != provider:
        raise LLMRoutingError(
            f"{where} : l'endpoint hébergé {family!r} contredit le fournisseur {provider!r} "
            "(la clé de ce rôle ne peut pas être envoyée à un autre fournisseur)"
        )
    return url


def _split(role: LLMRole | str, settings_obj: object):
    """(role, global_provider, global_model, role_provider, role_model, suffix) lus dans la config."""
    role_value = _norm_role(role)
    g_provider = _provider(_setting(settings_obj, "LLM_PROVIDER") or "gemini", where="LLM_PROVIDER")
    g_model = _setting(settings_obj, "LLM_MODEL")
    if role_value == LLMRole.DEFAULT.value:
        return role_value, g_provider, g_model, "", "", ""
    suffix = role_value.upper()
    return (
        role_value,
        g_provider,
        g_model,
        _provider(_setting(settings_obj, f"LLM_PROVIDER_{suffix}"), where=f"LLM_PROVIDER_{suffix}"),
        _setting(settings_obj, f"LLM_MODEL_{suffix}"),
        suffix,
    )


def _auth_selection(role_value: str, suffix: str, settings_obj: object) -> str:
    """Authentification demandée par l'opérateur : ``""`` (par défaut), ``subscription`` ou ``api_key``/``none``."""
    selected = _setting(settings_obj, f"LLM_AUTH_{suffix}").lower() if suffix else ""
    if selected and selected not in AUTH_METHODS:
        raise LLMRoutingError(f"LLM_AUTH_{suffix} : méthode inconnue (valeurs : {', '.join(AUTH_METHODS)})")
    if role_value == LLMRole.CODER.value and bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)):
        if selected and selected != AUTH_SUBSCRIPTION:
            raise LLMRoutingError(
                f"CODER_SUBSCRIPTION=true contredit LLM_AUTH_CODER={selected} : choisir UNE authentification"
            )
        return AUTH_SUBSCRIPTION
    return selected


def _provider_and_model(role: LLMRole | str, settings_obj: object) -> Tuple[str, str, str, str, str]:
    """``(role, provider, model canonique, suffix, auth_choisie)`` ; lève sur toute contradiction de configuration."""
    role_value, g_provider, g_model, r_provider, r_model, suffix = _split(role, settings_obj)
    where = f"rôle {role_value}"
    auth = _auth_selection(role_value, suffix, settings_obj)
    if auth == AUTH_SUBSCRIPTION:
        # Abonnement : réservé au fournisseur OpenAI (backend ChatGPT/Codex), jamais déduit du nom du modèle.
        if r_provider and r_provider != "openai":
            raise LLMRoutingError(
                f"{where} : l'abonnement n'existe que pour le fournisseur openai (reçu {r_provider!r})"
            )
        if role_value == LLMRole.CODER.value and bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)):
            model = _setting(settings_obj, "CODER_SUBSCRIPTION_MODEL") or "gpt-5.5"
            if r_model and canonical_model("openai", r_model, where=where) != model:
                raise LLMRoutingError(
                    f"{where} : CODER_SUBSCRIPTION=true impose CODER_SUBSCRIPTION_MODEL ; LLM_MODEL_CODER la "
                    "contredit (retirer l'un des deux)"
                )
        else:
            if not r_model:
                raise LLMRoutingError(f"{where} : un rôle par abonnement doit nommer son modèle (LLM_MODEL_{suffix})")
            model = r_model
        return role_value, "openai", canonical_model("openai", model, where=where), suffix, auth
    provider = r_provider or g_provider
    if not provider:
        raise LLMRoutingError(f"{where} : fournisseur absent")
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMRoutingError(
            f"{where} : fournisseur {provider!r} non supporté pour le sampling "
            f"(catalogue fermé : {', '.join(SUPPORTED_PROVIDERS)})"
        )
    if r_model:
        model = r_model
    elif provider != g_provider:
        raise LLMRoutingError(
            f"{where} : le fournisseur {provider!r} diffère du fournisseur global {g_provider!r} — nommer le modèle "
            f"du rôle (LLM_MODEL_{suffix}) ; le modèle global n'est jamais hérité d'un autre fournisseur"
        )
    else:
        model = g_model
    # Un modèle global VIDE reste « non configuré » (le transport utilisait son défaut) : seul resolve_route l'exige.
    return role_value, provider, canonical_model(provider, model, where=where) if model else "", suffix, auth


def resolve_role(role: LLMRole | str = LLMRole.DEFAULT, settings_obj: Optional[object] = None) -> Tuple[str, str]:
    """Retourne ``(provider, model canonique)`` pour un rôle ; lève :class:`LLMRoutingError` si la config se contredit.

    Le modèle est le nom NU (préfixe du fournisseur retiré). Ne vérifie PAS la présence d'une clé (voir
    :func:`resolve_route`). Sans configuration par rôle, renvoie le couple global.

    Args:
        role: rôle (``LLMRole`` ou sa valeur str).
        settings_obj: settings à interroger (défaut : le singleton ``settings``).
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    _role, provider, model, _suffix, _auth = _provider_and_model(role, settings_obj)
    return provider, model


def resolve_route(
    role: LLMRole | str = LLMRole.DEFAULT,
    settings_obj: Optional[object] = None,
    *,
    require_credential: bool = True,
) -> LLMRoute:
    """Destination EFFECTIVE d'un rôle : fournisseur, modèle, endpoint, authentification, provenance de la clé.

    ``require_credential=False`` valide la cohérence (fournisseur/modèle/endpoint) sans exiger la clé — pour un chemin
    dont le transport est délégué (client MCP externe). Le transport qui ÉMET appelle toujours la forme stricte.
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    role_value, provider, model, suffix, auth_choice = _provider_and_model(role, settings_obj)
    where = f"rôle {role_value}"
    if not model:
        raise LLMRoutingError(
            f"{where} : aucun modèle configuré (LLM_MODEL" + (f" ou LLM_MODEL_{suffix}" if suffix else "") + ")"
        )
    _r, g_provider, _gm, _rp, _rm, _s = _split(role, settings_obj)

    role_url = _setting(settings_obj, f"LLM_BASE_URL_{suffix}") if suffix else ""
    role_key = _setting(settings_obj, f"LLM_API_KEY_{suffix}") if suffix else ""
    global_key = _setting(settings_obj, "LLM_API_KEY")

    if auth_choice == AUTH_SUBSCRIPTION:
        if role_key:
            raise LLMRoutingError(f"{where} : une clé API est définie alors que l'authentification est l'abonnement")
        if role_url:
            raise LLMRoutingError(f"{where} : un endpoint est défini alors que l'authentification est l'abonnement")
        return LLMRoute(
            role=role_value,
            provider="openai",
            model=model,
            endpoint=None,
            explicit_endpoint=False,
            auth=AUTH_SUBSCRIPTION,
            credential_source=SOURCE_SUBSCRIPTION,
        )

    # ── endpoint ────────────────────────────────────────────────────────────────────────
    explicit = True
    if role_url:
        endpoint = role_url
    elif provider == g_provider and (_setting(settings_obj, "llm_base_url") or _setting(settings_obj, "LLM_BASE_URL")):
        # Même fournisseur que le global : son endpoint configuré s'applique, JAMAIS ignoré en silence. Un endpoint qui
        # désigne un autre fournisseur hébergé (ex. api.openai.com sous « gemini ») est refusé par ``_check_endpoint``.
        endpoint = _setting(settings_obj, "llm_base_url") or _setting(settings_obj, "LLM_BASE_URL")
    else:
        explicit = False
        endpoint = {
            "gemini": GEMINI_OPENAI_BASE_URL,
            "openai": OPENAI_BASE_URL,
        }.get(provider) or LOCAL_DEFAULT_BASE_URLS[provider]
    endpoint = _check_endpoint(provider, endpoint, where=where)

    # ── clé ─────────────────────────────────────────────────────────────────────────────
    if role_key:
        key, source = role_key, SOURCE_ROLE
    elif global_key and provider == g_provider:
        key, source = global_key, SOURCE_GLOBAL
    else:
        key, source = "", SOURCE_NONE
    if auth_choice == AUTH_NONE and key:
        raise LLMRoutingError(f"{where} : LLM_AUTH_{suffix}=none contredit une clé définie")
    if not key:
        if auth_choice == AUTH_NONE or (not auth_choice and provider in LOCAL_PROVIDERS):
            # Sans clé : seulement par CHOIX (``LLM_AUTH_<ROLE>=none``) ou par défaut d'un fournisseur local.
            if provider not in LOCAL_PROVIDERS:
                raise LLMRoutingError(f"{where} : le fournisseur hébergé {provider!r} exige une clé")
            auth = AUTH_NONE
        elif require_credential:
            # Une authentification ``api_key`` EXPLICITE sans clé effective n'est jamais dégradée en accès anonyme, même
            # pour un fournisseur local ; un fournisseur hébergé sans choix exige aussi une clé.
            hint = f"définir LLM_API_KEY_{suffix}" if suffix else "définir LLM_API_KEY"
            explicit = (
                f" (LLM_AUTH_{suffix}=api_key est explicite : retirer ce choix ou utiliser none pour un accès sans clé)"
                if auth_choice == AUTH_API_KEY and provider in LOCAL_PROVIDERS
                else ""
            )
            inherit = (
                f" ; la clé globale n'est héritée que pour le fournisseur global ({g_provider!r}), "
                f"pas pour {provider!r}"
                if provider != g_provider
                else ""
            )
            raise LLMMissingCredentialError(
                f"{where} : aucune clé pour le fournisseur {provider!r} — {hint}{explicit}{inherit}"
            )
        else:
            # ``require_credential=False`` : cohérence seulement (nom, préflight, tarification). La route reste
            # ``api_key`` SANS credential ; aucun transport émetteur ne l'accepte (``LLMRoute.transport_key``).
            auth = AUTH_API_KEY
    else:
        auth = AUTH_API_KEY
    return LLMRoute(
        role=role_value,
        provider=provider,
        model=model,
        endpoint=endpoint,
        explicit_endpoint=explicit,
        auth=auth,
        credential_source=source,
        credential_fingerprint=_fingerprint(key),
        _credential=key or None,
    )


def validate_role_routes(
    settings_obj: Optional[object] = None,
    roles: Optional[List[LLMRole | str]] = None,
    *,
    require_credential: bool = True,
) -> dict:
    """Préflight SANS émission ni dépense : ``{rôle: route.describe()}`` ou ``LLMRoutingError`` au premier refus.

    Réutilisable par un harnais pour vérifier, avant tout appel facturable, que chaque rôle réellement appelé a une
    destination, une identité et un modèle cohérents.
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    selected = roles if roles is not None else [r for r in LLMRole]
    return {
        _norm_role(role): resolve_route(role, settings_obj, require_credential=require_credential).describe()
        for role in selected
    }


def check_role_routes(settings_obj: Optional[object] = None, roles: Optional[List[LLMRole | str]] = None) -> dict:
    """Contrôle SANS émission ni exception de chaque rôle : ``{rôle: {"status", "route", "error"}}``.

    ``status`` : ``ok`` (route cohérente et credential présent, ou fournisseur local / abonnement), ``missing_credential``
    (cohérente mais sans clé de SON fournisseur : refusée à l'appel) ou ``invalid`` (contradiction fournisseur / modèle /
    endpoint / authentification, fournisseur non supporté : refus de démarrage). ``route`` est la vue sans secret
    (``LLMRoute.describe()``) quand elle est connue ; ``error`` ne contient jamais de secret.
    """
    if settings_obj is None:
        from collegue.config import settings as settings_obj

    report: dict = {}
    for role in roles if roles is not None else [r for r in LLMRole]:
        name = _norm_role(role)
        try:
            report[name] = {
                "status": "ok",
                "route": resolve_route(role, settings_obj, require_credential=True).describe(),
                "error": None,
            }
        except LLMMissingCredentialError as exc:
            report[name] = {"status": "missing_credential", "route": None, "error": str(exc)}
        except LLMRoutingError as exc:
            report[name] = {"status": "invalid", "route": None, "error": str(exc)}
    return report


# ── préférences de modèle porteuses du rôle ──────────────────────────────────────────────────────────


def route_hint(role: LLMRole | str) -> str:
    return f"{ROUTE_HINT_PREFIX}{_norm_role(role)}"


def parse_route_preferences(model_preferences: Any) -> Tuple[Optional[str], List[str]]:
    """``(rôle porté par le hint de route ou None, noms de modèles)`` d'après des ``model_preferences`` quelconques.

    Accepte une liste/tuple de noms, un nom, ou un objet ``ModelPreferences`` MCP (hints sérialisés) : le rôle voyage
    DANS les noms de hints, donc survit à la sérialisation jusqu'au handler serveur.
    """
    names: List[str] = []
    if isinstance(model_preferences, str):
        names = [model_preferences]
    elif isinstance(model_preferences, (list, tuple)):
        names = [str(item) for item in model_preferences]
    else:
        hints = getattr(model_preferences, "hints", None)
        if hints:
            names = [str(getattr(hint, "name", "") or "") for hint in hints]
    role: Optional[str] = None
    models: List[str] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name.startswith(ROUTE_HINT_PREFIX):
            carried = name[len(ROUTE_HINT_PREFIX) :].strip().lower()
            if role is not None and role != carried:
                raise LLMRoutingError("model_preferences porte plusieurs rôles contradictoires")
            role = carried or None
        else:
            models.append(name)
    return role, models
