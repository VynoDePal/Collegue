"""Mesure des métriques de qualité du projet généré (G1, epic #382, Phase 4).

Mesure **déterministe et projet-scopée** de la qualité du projet généré, agrégée
en un **score composite** pluggable. L'objectif est mesuré sur le **workspace sur
disque** (et non sur le diff d'une itération) pour rester **symétrique** avant/après
— c'est ce qui permet à la boucle G4 de promouvoir un vrai gain sans faux-rejet
(cf. #541) :

* **couverture de tests ↑** (parsée de la sortie pytest-cov) ;
* **sécurité ↓** : compte de secrets **pondéré par sévérité**, issu d'un scan
  **statique** (``secret_scan``, moteur regex, zéro LLM) sur le répertoire du
  projet — déterministe par construction ;
* ``tests_passed`` comme **garde dure** (utilisée par le gate G2).

La **revue LLM** (``review_score``) est conservée à titre **informatif** (corps de
PR, relecteur humain) mais **n'entre pas** dans le composite gaté : un signal LLM
diff-scopé est non déterministe et asymétrique avant/après (la cause racine du
faux-rejet v9). Le « score du dashboard » du serveur (latence/coût de SES experts)
ne convient pas non plus : on mesure la qualité du **projet généré**.

Module **isolé** : non câblé au runtime (la boucle G4 l'orchestre).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

# Commande de couverture par défaut (parsée depuis la sortie pytest-cov).
DEFAULT_COVERAGE_COMMAND = "pytest -q --cov --cov-report=term-missing"

# Pondération par sévérité des findings de sécurité (secret_scan). Le critique pèse
# le plus ; le composite et le gate (tolérance 0) raisonnent sur ce total pondéré.
SECURITY_SEVERITY_WEIGHTS = {"critical": 10.0, "high": 5.0, "medium": 2.0, "low": 1.0}

# Fichiers/dossiers EXCLUS du scan sécu de la BOUCLE (#547). Les lockfiles générés et
# les emplacements de test/fixtures/exemples contiennent légitimement des chaînes qui
# ressemblent à des secrets (URLs de registre npm, faux tokens, sqlite:/// de fixtures)
# qu'on ne « corrige » jamais — sans exclusion ils noient le signal (sur un MVP réel,
# ~99 % du poids sécu venait de package-lock.json). Conventions GÉNÉRIQUES, multi-
# langage, sans hypothèse produit. Lockfiles alignés sur ``_GENERATED_DIFF_FILES``
# (#526). C'est propre à la **fonction objectif de la boucle** (pas un audit de
# sécurité exhaustif) : un secret en fixture/test n'atteint pas le runtime produit.
# Extensions de test explicites (pas ``*.test.*`` / ``*.spec.*``) pour ne pas exclure
# un contrat produit type ``openapi.spec.yaml`` / ``manifest.test.json``.
SECURITY_SCAN_EXCLUDES = (
    # lockfiles générés
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    # dossiers de test / fixtures (conventions multi-langage)
    "tests",
    "test",
    "__tests__",
    "fixtures",
    # fichiers de test (conventions par langage, extensions explicites)
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "conftest.py",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
    "*.spec.jsx",
    # gabarits/exemples de config (placeholders, jamais des secrets réels)
    "*.example",
    "*.sample",
    "*.template",
)

# Règles ruff comptées comme « violations de lint » (erreurs pyflakes/pycodestyle).
DEFAULT_LINT_SELECT = ("E", "F", "W")
# Seuil de complexité cyclomatique (mccabe / ruff C901) : au-delà = « bloc complexe ».
DEFAULT_COMPLEXITY_MAX = 10

# Regex de la ligne récapitulative TOTAL de pytest-cov : « TOTAL  120  6  95% ».
# On exige un espace juste après TOTAL (rejette un fichier nommé « TOTAL.py »).
_TOTAL_RE = re.compile(r"^\s*TOTAL\s+.*?(\d+(?:\.\d+)?)%", re.MULTILINE)


@dataclass(frozen=True)
class CompositeWeights:
    """Pondérations du score composite (extensibles).

    La couverture domine ; lint/complexité sont des pénalités faibles (un gain de
    couverture ne doit pas être annulé par une violation de lint marginale).
    """

    coverage: float = 1.0  # par point de couverture normalisé (0–1)
    security: float = 0.1  # pénalité par unité de score sécu pondéré
    lint: float = 0.02  # pénalité par violation de lint
    complexity: float = 0.05  # pénalité par bloc trop complexe


DEFAULT_WEIGHTS = CompositeWeights()


@dataclass(frozen=True)
class ProjectQualityMetrics:
    """Instantané des métriques de qualité d'un projet (à un instant/itération)."""

    coverage_pct: float  # 0–100 (0.0 si non mesurée — voir coverage_measured)
    security_findings: int  # compte BRUT de secrets (proposeur + corps de PR)
    security_weighted: float  # score sécu pondéré par sévérité (composite + gate)
    tests_passed: bool
    composite: float
    # False si la couverture n'a PAS pu être mesurée (pas de ligne TOTAL). Le gate
    # (G2) doit alors traiter le delta de couverture comme inconnu (fail-closed),
    # plutôt que de confondre « non mesuré » avec « 0 % réel ».
    coverage_measured: bool = True
    # INFORMATIF (hors-gate) : score de revue LLM pour le corps de PR. N'entre PAS
    # dans ``composite`` (un signal LLM diff-scopé est non déterministe — #541).
    review_score: float = 0.0
    # Signaux qualité déterministes (ruff/mccabe, #543). 0 = neutre (projet non-Python
    # où ruff tourne sans erreur) ; un signal qualité non mesurable ne bloque pas (≠ sécu).
    lint_violations: int = 0
    complexity_bad_blocks: int = 0
    # False si ruff n'a pas pu tourner (absent / panne). Le gate rejette une bascule
    # avant≠après (sinon un échec de scan après gonflerait le composite — #543).
    quality_measured: bool = True


def parse_coverage(output: str) -> Optional[float]:
    """Extrait le pourcentage de couverture de la ligne ``TOTAL`` de pytest-cov.

    Retourne ``None`` si aucune ligne ``TOTAL … NN%`` n'est présente (sortie
    tronquée, pas de couverture mesurée…). Gère les pourcentages décimaux.
    """
    if not output:
        return None
    match = _TOTAL_RE.search(output)
    return float(match.group(1)) if match else None


def composite_score(
    coverage_pct: float,
    security_weighted: float,
    *,
    lint_violations: int = 0,
    complexity_bad_blocks: int = 0,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
) -> float:
    """Score composite pondéré (déterministe).

    Monotone : ↑couverture → ↑ ; ↑sécu pondérée / ↑lint / ↑complexité → ↓. La
    couverture (0–100) est normalisée en 0–1 ; sécu/lint/complexité sont des
    pénalités. La revue LLM n'y figure pas (informative, hors-gate).
    """
    return (
        weights.coverage * (coverage_pct / 100.0)
        - weights.security * security_weighted
        - weights.lint * lint_violations
        - weights.complexity * complexity_bad_blocks
    )


def _default_security_scan(workspace: str) -> Tuple[int, float]:
    """Scan statique de secrets sur le RÉPERTOIRE du projet (déterministe, sans LLM).

    Mesure interne au moteur (pas une requête MCP) : on désarme rate-limit/quotas
    (état global non déterministe) ; le scan reste 100 % statique (moteur regex).
    Retourne ``(compte_total, score_pondéré_par_sévérité)``.
    """
    from collegue.tools.secret_scan.tool import SecretScanTool

    tool = SecretScanTool()
    tool.rate_limit_enabled = False
    tool.quota_enabled = False
    resp = tool.execute(
        {"target": workspace, "scan_type": "directory", "exclude_patterns": list(SECURITY_SCAN_EXCLUDES)}
    )
    weighted = (
        SECURITY_SEVERITY_WEIGHTS["critical"] * resp.critical
        + SECURITY_SEVERITY_WEIGHTS["high"] * resp.high
        + SECURITY_SEVERITY_WEIGHTS["medium"] * resp.medium
        + SECURITY_SEVERITY_WEIGHTS["low"] * resp.low
    )
    return int(resp.total_findings), float(weighted)


def _scan_security(workspace: str, *, scan_fn=None) -> Tuple[int, float]:
    """Mesure sécu déterministe (injectable). Échec ⇒ ``(-1, inf)`` (fail-closed).

    Un score pondéré ``inf`` rend le composite non fini → le gate (G2) rejette le
    round : on ne promeut jamais un changement dont on n'a pas pu mesurer la sécu.
    """
    fn = scan_fn or _default_security_scan
    try:
        total, weighted = fn(workspace)
        return int(total), float(weighted)
    except Exception:  # noqa: BLE001 — toute panne de scan ⇒ fail-closed
        return -1, math.inf


def _find_ruff() -> Optional[str]:
    """Localise l'exécutable ruff : PATH, puis à côté de l'interpréteur (venv)."""
    found = shutil.which("ruff")
    if found:
        return found
    candidate = os.path.join(os.path.dirname(sys.executable), "ruff")
    return candidate if os.path.exists(candidate) else None


def _ruff_count(ruff: str, workspace: str, select_args) -> int:
    """Compte les diagnostics ruff (JSON) pour une sélection de règles, sur le workspace.

    ``--isolated`` ignore la config ruff du PROJET GÉNÉRÉ → mesure reproductible et
    indépendante du projet ; ``--no-cache`` évite toute pollution inter-runs ;
    ``timeout`` borne le temps. Toute panne (timeout, JSON illisible) **se propage**
    → traitée comme « non mesuré » par :func:`_scan_quality` (et non comme un faux 0).
    """
    proc = subprocess.run(
        [ruff, "check", workspace, "--isolated", *select_args, "--output-format=json", "--no-cache"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return len(json.loads(proc.stdout or "[]"))


def _default_quality_scan(
    workspace: str,
    *,
    lint_select=DEFAULT_LINT_SELECT,
    complexity_max: int = DEFAULT_COMPLEXITY_MAX,
) -> Tuple[int, int, bool]:
    """Lint + complexité déterministes via ruff (mccabe), sur le workspace.

    Retourne ``(lint_violations, complexity_bad_blocks, measured)``. ruff absent ⇒
    ``(0, 0, False)`` (non mesuré). Un projet non-Python où ruff tourne sans erreur
    ⇒ ``(0, 0, True)`` (mesuré, neutre). Une panne de scan se propage (→ non mesuré).
    """
    ruff = _find_ruff()
    if not ruff:
        return 0, 0, False
    lint = _ruff_count(ruff, workspace, ["--select", ",".join(lint_select)])
    complexity = _ruff_count(
        ruff, workspace, ["--select", "C901", "--config", f"lint.mccabe.max-complexity={complexity_max}"]
    )
    return lint, complexity, True


def _scan_quality(workspace: str, *, scan_fn=None) -> Tuple[int, int, bool]:
    """Mesure lint/complexité déterministe (injectable). Échec ⇒ ``(0, 0, False)``.

    ``measured`` distingue « ruff a tourné » de « non mesuré » (ruff absent / panne) :
    le gate (G2) rejette une **bascule de mesurabilité** (avant≠après) — sinon un échec
    de scan APRÈS (lint→0) gonflerait le composite et promouvrait à tort (#543). Hors
    bascule, un signal qualité non mesurable reste neutre (≠ sécu, fail-closed dur).
    """
    fn = scan_fn or _default_quality_scan
    try:
        lint, complexity, measured = fn(workspace)
        return int(lint), int(complexity), bool(measured)
    except Exception:  # noqa: BLE001 — panne de scan ⇒ non mesuré (le gate gère la bascule)
        return 0, 0, False


def autofix_lint(workspace: str, files, *, lint_select=DEFAULT_LINT_SELECT) -> int:
    """Auto-corrige le lint des fichiers Python touchés (ruff --fix + format), in-place (#549).

    Appelé par la boucle APRÈS le diff du coder et AVANT la mesure : le coder se
    concentre sur le fond, le lint auto-corrigible (imports inutilisés, espaces, mise
    en forme) est nettoyé → une amélioration de couverture/refactor n'est pas bloquée
    par du lint résiduel (le gate étant tolérance-0 sur le lint).

    Déterministe et générique : ``--isolated`` (ignore la config du projet généré) ;
    scopé aux ``.py`` réellement présents parmi ``files`` ; ruff absent ou projet
    non-Python ⇒ **no-op** (renvoie 0). Best-effort (toute panne ruff ignorée). Sûr :
    un fix qui casserait un test est rattrapé par la mesure ``after`` (tests rouges ⇒
    le gate rejette) — on ne promeut jamais un fix cassant. Renvoie le nb de fichiers
    Python traités.
    """
    ruff = _find_ruff()
    if not ruff:
        return 0
    py = [os.path.join(workspace, f) for f in files if f.endswith(".py") and os.path.isfile(os.path.join(workspace, f))]
    if not py:
        return 0
    try:
        subprocess.run(
            [ruff, "check", *py, "--isolated", "--select", ",".join(lint_select), "--fix", "--no-cache"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run([ruff, "format", *py, "--isolated"], capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001 — auto-fix best-effort, jamais bloquant
        pass
    return len(py)


async def measure(
    workspace: str,
    ctx,
    *,
    sandbox,
    reviewer=None,
    diff: str = "",
    issue=None,
    coverage_command: str = DEFAULT_COVERAGE_COMMAND,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
    security_scan_fn=None,
    quality_scan_fn=None,
) -> ProjectQualityMetrics:
    """Mesure couverture + sécu + lint/complexité (déterministes) → composite.

    ``sandbox`` est injectable (mocké en CI) ; la couverture vient du parsing de la
    sortie pytest-cov et ``tests_passed`` = exit 0. Sécu (``security_scan_fn``) et
    lint/complexité (``quality_scan_fn``) viennent de scans **statiques déterministes**
    du **workspace** (injectables en tests). La revue LLM (``reviewer``/``diff``) est
    **optionnelle et informative** (corps de PR) : elle n'entre pas dans le composite.
    """
    test_res = sandbox.run_tests(workspace, coverage_command)
    tests_passed = bool(test_res.ok)
    raw_coverage = parse_coverage(test_res.stdout)
    coverage_measured = raw_coverage is not None
    coverage_pct = raw_coverage if coverage_measured else 0.0  # composite conservateur

    security_findings, security_weighted = _scan_security(workspace, scan_fn=security_scan_fn)
    lint_violations, complexity_bad_blocks, quality_measured = _scan_quality(workspace, scan_fn=quality_scan_fn)

    # Revue LLM : INFORMATIVE uniquement (hors composite). Calculée seulement s'il y
    # a un reviewer ET un diff à examiner ; toute panne reste sans effet sur le gate.
    review_score = 0.0
    if reviewer is not None and diff:
        try:
            outcome = await reviewer.review(diff, ctx, issue=issue)
            review_score = float(getattr(outcome, "quality_score", 0.0))
        except Exception:  # noqa: BLE001 — la revue est informative, jamais bloquante
            review_score = 0.0

    return ProjectQualityMetrics(
        coverage_pct=coverage_pct,
        security_findings=security_findings,
        security_weighted=security_weighted,
        tests_passed=tests_passed,
        composite=composite_score(
            coverage_pct,
            security_weighted,
            lint_violations=lint_violations,
            complexity_bad_blocks=complexity_bad_blocks,
            weights=weights,
        ),
        coverage_measured=coverage_measured,
        review_score=review_score,
        lint_violations=lint_violations,
        complexity_bad_blocks=complexity_bad_blocks,
        quality_measured=quality_measured,
    )


def persist(manager, project_id: int, metrics: ProjectQualityMetrics) -> None:
    """Enregistre les métriques (modèle ``Metric``, C6) pour suivi/itérations."""
    manager.add_metric(project_id, "coverage_pct", metrics.coverage_pct)
    manager.add_metric(project_id, "security_findings", float(metrics.security_findings))
    manager.add_metric(project_id, "security_weighted", metrics.security_weighted)
    manager.add_metric(project_id, "tests_passed", 1.0 if metrics.tests_passed else 0.0)
    manager.add_metric(project_id, "coverage_measured", 1.0 if metrics.coverage_measured else 0.0)
    manager.add_metric(project_id, "review_score", metrics.review_score)
    manager.add_metric(project_id, "lint_violations", float(metrics.lint_violations))
    manager.add_metric(project_id, "complexity_bad_blocks", float(metrics.complexity_bad_blocks))
    manager.add_metric(project_id, "quality_measured", 1.0 if metrics.quality_measured else 0.0)
    manager.add_metric(project_id, "composite", metrics.composite)
