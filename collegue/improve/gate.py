"""Gate par métrique — décision de promotion d'un diff (G2, epic #382, Phase 4).

Compare deux instantanés :class:`ProjectQualityMetrics` (avant/après un diff
d'amélioration) et décide si le changement doit être **promu en PR**. **Fail-closed** :
on ne promeut que si l'objectif progresse **sans régression** ; toute incertitude
(tests rouges, mesurabilité de la couverture qui change, sécu aggravée, gain sous
le bruit) ⇒ **rejet** (le diff est jeté — « rollback » avant promotion).

Gate **AVANT la PR** (décision epic) : aucune régression ne peut donc atteindre
``main`` ; le merge de la PR promue reste **humain** (§6).

Fonction **pure** : aucun effet de bord, entièrement testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from collegue.improve.metrics import ProjectQualityMetrics

# Gain composite minimal pour promouvoir (évite de promouvoir du bruit / surplace).
DEFAULT_MIN_GAIN = 0.01


@dataclass(frozen=True)
class GateDecision:
    """Verdict de promotion d'un diff d'amélioration."""

    accepted: bool
    reason: str
    delta: float  # composite après − avant
    before: ProjectQualityMetrics
    after: ProjectQualityMetrics


def evaluate(
    before: ProjectQualityMetrics,
    after: ProjectQualityMetrics,
    *,
    min_gain: float = DEFAULT_MIN_GAIN,
) -> GateDecision:
    """Décide si l'amélioration (``before`` → ``after``) est promue. Fail-closed.

    Règles (toutes requises pour accepter) :

    1. **Garde dure** : ``after.tests_passed`` (tests rouges ⇒ rejet immédiat).
    2. **Scores finis** : ``before``/``after`` composites finis — un score ``NaN``/``inf``
       (mesure corrompue) passerait sinon la garde de gain (``NaN < x`` est ``False``)
       et ferait échouer le fail-closed ⇒ rejet.
    3. **Mesurabilité stable** : ``before.coverage_measured == after.coverage_measured``
       — un changement qui casse/active la mesure de couverture rend le delta de
       couverture non fiable (cf. G1) ⇒ rejet.
    4. **Pas de régression sécu** : ``after.security_weighted <= before.security_weighted``
       (tolérance 0 — toute aggravation sécu pondérée est un rejet dur). Un échec de
       scan sécu produit un ``security_weighted`` non fini ⇒ composite non fini ⇒
       rejet à la règle 2 (fail-closed).
    5. **Gain réel** : ``after.composite >= before.composite + min_gain``.

    ``min_gain`` négatif inverserait la garde « pas de régression » → borné à 0.
    """
    min_gain = max(0.0, min_gain)
    delta = after.composite - before.composite

    def reject(reason: str) -> GateDecision:
        return GateDecision(accepted=False, reason=reason, delta=delta, before=before, after=after)

    if not after.tests_passed:
        return reject("tests rouges après le diff (garde dure)")
    if not (math.isfinite(before.composite) and math.isfinite(after.composite)):
        return reject("score composite non fini (NaN/inf) — mesure non fiable")
    if before.coverage_measured != after.coverage_measured:
        return reject("mesurabilité de la couverture instable (avant≠après) — delta non fiable")
    if after.security_weighted > before.security_weighted:
        return reject(f"régression sécu : {after.security_weighted:.1f} > {before.security_weighted:.1f} (pondéré)")
    if delta < min_gain:
        return reject(f"gain insuffisant : Δ={delta:.4f} < min_gain={min_gain:.4f}")

    return GateDecision(
        accepted=True,
        reason=f"gain composite Δ={delta:.4f} sans régression",
        delta=delta,
        before=before,
        after=after,
    )
