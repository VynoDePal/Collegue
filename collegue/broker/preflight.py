"""Préflight LÉGER du transport courtier — pour B (campagne) et pour les opérateurs : capacité établie sans clé, sans inférence.

``preflight_broker_transport`` contrôle, à partir de la configuration SEULE et du sandbox réellement instancié :

1. le contrat de configuration (Google, deux Gemma officiels, repli = le 26B du codeur, aucun mode de substitution) ;
2. la cohérence locale des routes de tous les rôles (aucune requête) ;
3. le prix 0 attesté des deux identités sur l'endpoint officiel (grille de prix du produit) ;
4. la PREUVE de transport du worker (réseau none, socket unique en lecture seule, aucune clé / proxy / passthrough) ;
5. (si un registre est fourni) la présence des tables du courtier (migration 0013) ;
6. l'échéance globale (exigée ou informative) ;
7. la PRÉSENCE de la clé chez le service de confiance : **information** par défaut (``required=False`` : les préflights statique
   et complet réussissent SANS clé, aucune clé factice n'est injectée) ; **exigence au lancement** avec ``require_provider_key``.

Le préflight n'émet JAMAIS de génération et n'appelle jamais Google. La qualification effective des DEUX modèles (texte, JSON, outils)
est une étape distincte, réelle et obligatoire : :meth:`collegue.broker.BrokerService.qualify_models`.

``build_preflight_agent`` fabrique le VRAI ``OHSdkAgent`` du produit (sandbox réel du pilote, runtime du courtier) : appeler
``allocate_worker`` dessus sur un registre temporaire évalue exactement le transport qui serait utilisé.
``capability_proof`` est l'interface consommée par B (``collegue.broker.capability_proof``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Tuple

from collegue.broker.capability import TransportCheck, TransportProof
from collegue.broker.policy import OFFICIAL_MODELS
from collegue.broker.runtime import BrokerConfigurationError, BrokerRuntime, validate_broker_settings


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    checks: Tuple[TransportCheck, ...]
    transport: str = "budget_broker"

    @property
    def failures(self) -> List[str]:
        return [f"{c.name}: {c.detail}" if c.detail else c.name for c in self.checks if c.required and not c.ok]

    def to_dict(self) -> dict:
        return {
            "transport": self.transport,
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required} for c in self.checks],
        }


def build_preflight_agent(settings: Any, *, sandbox: Optional[Any] = None, runtime: Optional[BrokerRuntime] = None):
    """Le vrai ``OHSdkAgent`` du produit en mode courtier (pur : aucun Docker lancé, aucune requête, aucune clé exposée)."""
    from collegue.executor.openhands_sdk_agent import OHSdkAgent
    from collegue.pilot import runtime as pilot_runtime
    from collegue.sandbox import DockerSandbox

    validate_broker_settings(settings)
    box = sandbox if sandbox is not None else DockerSandbox(**pilot_runtime._coder_sandbox_kwargs(settings))
    return OHSdkAgent(box, settings_obj=settings, broker=runtime or BrokerRuntime.from_settings(settings))


def _key_present(settings: Any) -> bool:
    key = getattr(settings, "LLM_API_KEY", "")
    reveal = getattr(key, "get_secret_value", None)
    return bool(reveal() if callable(reveal) else key)


def preflight_broker_transport(
    settings: Any,
    *,
    sandbox: Optional[Any] = None,
    ledger: Optional[Any] = None,
    runtime: Optional[BrokerRuntime] = None,
    require_global_deadline: bool = False,
    require_provider_key: bool = False,
    provider_probe: Optional[Callable[[], Any]] = None,
) -> PreflightReport:
    checks: List[TransportCheck] = []

    def add(name: str, ok: bool, detail: str = "", *, required: bool = True) -> bool:
        checks.append(TransportCheck(name, bool(ok), "" if ok else detail, required))
        return bool(ok)

    try:
        validate_broker_settings(settings)
        add("configuration_contract", True)
    except BrokerConfigurationError as exc:
        add("configuration_contract", False, str(exc))
        return PreflightReport(False, tuple(checks))

    from collegue.core.llm.roles import check_role_routes
    from collegue.monitoring.pricing import is_explicitly_free

    report = check_role_routes(settings)
    invalid = {role: item["error"] for role, item in report.items() if item["status"] == "invalid"}
    add("role_routes_coherent", not invalid, f"routes incohérentes : {invalid}")
    add(
        "models_explicitly_free_on_official_endpoint",
        all(is_explicitly_free(model, provider="gemini") for model in OFFICIAL_MODELS),
        "identité absente de la grille gratuite",
    )
    present = _key_present(settings)
    # Information par défaut : la capacité du transport ne dépend pas de la clé (aucune clé factice n'est jamais injectée).
    add(
        "trusted_service_has_a_key",
        present,
        "aucune clé Google chez le service de confiance (LLM_API_KEY)",
        required=bool(require_provider_key),
    )

    try:
        agent = build_preflight_agent(settings, sandbox=sandbox, runtime=runtime)
        proof: TransportProof = agent.broker_transport_proof()
        add("worker_transport_proof", proof.ok, "; ".join(proof.failures))
        add(
            "agent_declares_broker_capability",
            agent.budget_enforcement == "broker",
            "l'agent ne déclare pas la capacité",
        )
    except Exception as exc:  # noqa: BLE001 - toute erreur de fabrication est une capacité non établie
        add("worker_transport_proof", False, f"{type(exc).__name__}: {exc}")

    if ledger is not None:
        from sqlalchemy import inspect

        try:
            tables = set(inspect(ledger._session_factory.kw["bind"]).get_table_names())
            missing = sorted({"broker_sessions", "broker_attempts", "broker_clocks", "broker_owners"} - tables)
            add("broker_tables_present", not missing, f"migration 0013 absente : {missing}")
        except Exception as exc:  # noqa: BLE001
            add("broker_tables_present", False, f"{type(exc).__name__}")
    seconds = int(getattr(settings, "BROKER_GLOBAL_DEADLINE_SECONDS", 0) or 0)
    add(
        "global_deadline_configured",
        seconds > 0,
        "BROKER_GLOBAL_DEADLINE_SECONDS non configuré",
        required=bool(require_global_deadline),
    )
    if provider_probe is not None:
        try:
            provider_probe()
            add("provider_probe", True)
        except Exception as exc:  # noqa: BLE001
            add("provider_probe", False, f"{type(exc).__name__}")
    return PreflightReport(all(c.ok for c in checks if c.required), tuple(checks))


def capability_proof(
    settings: Any,
    *,
    sandbox: Optional[Any] = None,
    ledger: Optional[Any] = None,
    runtime: Optional[BrokerRuntime] = None,
    require_global_deadline: bool = False,
    require_provider_key: bool = False,
) -> Mapping[str, Any]:
    """Interface PUBLIQUE de capacité (consommée par B) : ``{"transport": "budget_broker", "accepted": bool, "reason": str, …}``.

    ``accepted`` est ``True`` seulement si TOUTES les vérifications requises passent sur le transport réellement instancié
    (sandbox du produit, runtime du courtier). Sans clé : acceptée si le transport est prouvé (``provider_key_present`` n'est
    qu'une information). Aucun champ ne contient de secret.
    """
    report = preflight_broker_transport(
        settings,
        sandbox=sandbox,
        ledger=ledger,
        runtime=runtime,
        require_global_deadline=require_global_deadline,
        require_provider_key=require_provider_key,
    )
    return {
        "transport": "budget_broker",
        "accepted": bool(report.ok),
        "reason": "; ".join(report.failures),
        "provider_key_present": _key_present(settings),
        "global_deadline_seconds": int(getattr(settings, "BROKER_GLOBAL_DEADLINE_SECONDS", 0) or 0),
        "models": list(OFFICIAL_MODELS),
        "checks": report.to_dict()["checks"],
    }
