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

# Tolérances de régression PAR SIGNAL qualité (#543). 0 = aucune régression tolérée.
# (La sécu n'a pas de slack : tolérance 0 dure, non configurable.)
DEFAULT_LINT_SLACK = 0
DEFAULT_COMPLEXITY_SLACK = 0


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
    lint_slack: int = DEFAULT_LINT_SLACK,
    complexity_slack: int = DEFAULT_COMPLEXITY_SLACK,
) -> GateDecision:
    """Décide si l'amélioration (``before`` → ``after``) est promue. Fail-closed.

    Règles (toutes requises pour accepter) :

    1. **Garde dure** : ``after.tests_passed`` (tests rouges ⇒ rejet immédiat).
    2. **Scores finis** : ``before``/``after`` composites finis — un score ``NaN``/``inf``
       (mesure corrompue) passerait sinon la garde de gain (``NaN < x`` est ``False``)
       et ferait échouer le fail-closed ⇒ rejet.
    3. **Mesurabilité stable** : ``before.coverage_measured == after.coverage_measured``
       ET ``before.quality_measured == after.quality_measured`` — une bascule de
       mesurabilité (couverture OU lint/complexité) rend le delta non fiable et, pour
       la qualité, gonflerait le composite (échec de scan après ⇒ pénalité retirée) ⇒
       rejet (cf. G1, #543).
    4. **Anti-régression multi-signal** (par signal, avec tolérance — #543) :
       - sécu : ``after.security_weighted <= before.security_weighted`` (tolérance 0
         dure ; un scan sécu en échec ⇒ composite non fini ⇒ rejet à la règle 2) ;
       - lint : ``after.lint_violations <= before.lint_violations + lint_slack`` ;
       - complexité : ``after.complexity_bad_blocks <= before.complexity_bad_blocks
         + complexity_slack``.
    5. **Gain réel** : ``after.composite >= before.composite + min_gain``.

    ``min_gain`` négatif inverserait la garde « pas de régression » → borné à 0 ; les
    slacks négatifs sont bornés à 0 (un slack < 0 rejetterait une amélioration).
    """
    min_gain = max(0.0, min_gain)
    lint_slack = max(0, lint_slack)
    complexity_slack = max(0, complexity_slack)
    delta = after.composite - before.composite

    def reject(reason: str) -> GateDecision:
        return GateDecision(accepted=False, reason=reason, delta=delta, before=before, after=after)

    if not after.tests_passed:
        return reject("tests rouges après le diff (garde dure)")
    if not (math.isfinite(before.composite) and math.isfinite(after.composite)):
        return reject("score composite non fini (NaN/inf) — mesure non fiable")
    if before.coverage_measured != after.coverage_measured:
        return reject("mesurabilité de la couverture instable (avant≠après) — delta non fiable")
    if before.quality_measured != after.quality_measured:
        return reject("mesurabilité qualité instable (avant≠après) — lint/complexité non fiables")
    if after.security_weighted > before.security_weighted:
        return reject(f"régression sécu : {after.security_weighted:.1f} > {before.security_weighted:.1f} (pondéré)")
    if after.lint_violations > before.lint_violations + lint_slack:
        return reject(f"régression lint : {after.lint_violations} > {before.lint_violations} (slack {lint_slack})")
    if after.complexity_bad_blocks > before.complexity_bad_blocks + complexity_slack:
        return reject(
            f"régression complexité : {after.complexity_bad_blocks} > "
            f"{before.complexity_bad_blocks} (slack {complexity_slack})"
        )
    if delta < min_gain:
        return reject(f"gain insuffisant : Δ={delta:.4f} < min_gain={min_gain:.4f}")

    return GateDecision(
        accepted=True,
        reason=f"gain composite Δ={delta:.4f} sans régression",
        delta=delta,
        before=before,
        after=after,
    )
