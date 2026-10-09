"""Erreurs du courtier : chacune porte un code stable et un statut HTTP, jamais un secret.

Un refus de POLITIQUE (requête hors liste blanche, contradictoire, trop grosse, échéance, budget) est levé AVANT toute
émission. Une violation de BORNE ou un usage incohérent est levé APRÈS une émission possible : elle bloque et signale, elle
n'est jamais réduite (min/clamp) ni présentée comme un succès.
"""

from __future__ import annotations

from typing import Optional


class BrokerError(Exception):
    """Base : ``code`` stable, ``status`` HTTP renvoyé au client, ``retryable`` (le client peut renvoyer la MÊME requête)."""

    code = "broker_error"
    status = 500
    retryable = False

    def __init__(self, message: str, *, code: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_openai_error(self) -> dict:
        """Corps d'erreur au format OpenAI (compatible clients/SDK) ; le message ne contient jamais de secret."""
        return {"error": {"message": str(self), "type": "collegue_broker_error", "code": self.code}}


class BrokerRequestRefused(BrokerError):
    """Requête invalide ou hors politique : refusée avant tout appel fournisseur."""

    code = "request_refused"
    status = 400


class BrokerAuthError(BrokerError):
    """Jeton absent, inconnu ou d'une autre session."""

    code = "unauthorized"
    status = 401


class BrokerForbidden(BrokerError):
    """Droit refusé côté serveur (rôle, modèle, session fermée, échéance)."""

    code = "forbidden"
    status = 403


class BrokerBudgetRefused(BrokerError):
    """Le registre refuse la réservation (plafond, scope bloqué, indisponible) : rien n'est émis."""

    code = "budget_refused"
    status = 403


class BrokerBlocked(BrokerError):
    """Un usage inconnu / une borne démentie bloque la session et le projet : plus aucune émission."""

    code = "budget_blocked"
    status = 403


class BrokerBoundViolation(BrokerBlocked):
    """Le fournisseur a démenti une borne (entrée, sortie, usage absent ou incohérent) : bloqué, signalé."""

    code = "bound_violation"
    status = 502


class BrokerUpstreamRejected(BrokerError):
    """Rejet DÉMONTRÉ avant traitement par le fournisseur (4xx/429) : réservation libérée."""

    code = "upstream_rejected"
    status = 502

    def __init__(self, message: str, *, upstream_status: int):
        super().__init__(message, status=429 if upstream_status == 429 else 502)
        self.upstream_status = upstream_status
        self.retryable = upstream_status == 429


class BrokerUpstreamAmbiguous(BrokerBlocked):
    """Échec APRÈS émission possible (5xx, timeout, connexion coupée) : usage inconnu, réservation conservée."""

    code = "upstream_ambiguous"
    status = 502


class BrokerUnsupported(BrokerRequestRefused):
    """Fonction non couverte (streaming, médias, URL arbitraires, countTokens indisponible…) : refus explicite."""

    code = "unsupported"
