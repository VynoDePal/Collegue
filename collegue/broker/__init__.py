"""Courtier budgétaire W5 : service de confiance qui détient seul la clé Google et rend le budget EFFECTIF.

Voir ``docs/consolidation/w5-broker.md``. API publique : :class:`BrokerService` (sessions, générations, fermeture,
réparation), :class:`GoogleUpstream` (client natif Google), la politique (:mod:`collegue.broker.policy`) et le contrôle de
capacité du transport (:mod:`collegue.broker.capability`).
"""

from collegue.broker.errors import (  # noqa: F401
    BrokerAuthError,
    BrokerBlocked,
    BrokerBoundViolation,
    BrokerBudgetRefused,
    BrokerError,
    BrokerForbidden,
    BrokerRequestRefused,
    BrokerUnsupported,
    BrokerUpstreamAmbiguous,
    BrokerUpstreamRejected,
)
from collegue.broker.service import BrokerConfig, BrokerService, OpenedSession, SessionSummary  # noqa: F401
from collegue.broker.upstream import GoogleUpstream  # noqa: F401
