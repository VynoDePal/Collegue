"""Mesure des métriques de qualité du projet généré (G1, epic #382, Phase 4).

Mesure **déterministe et projet-scopée** de la qualité du projet généré, agrégée
en un **score composite** pluggable. L'objectif est mesuré sur le **workspace sur
disque** (et non sur le diff d'une itération) pour rester **symétrique** avant/après
— c'est ce qui permet à la boucle G4 de promouvoir un vrai gain sans faux-rejet
(cf. #541) :

* **couverture de tests ↑** (parsée de la sortie : pytest-cov/coverage.py, go, JS-TS
  istanbul/jest/vitest, lcov, cobertura XML — cf. ``parse_coverage``, #577) ;
* **sécurité ↓** : compte de secrets **pondéré par sévérité**, issu d'un scan
  **statique** (``secret_scan``, moteur regex, zéro LLM) sur le répertoire du
  projet — déterministe par construction ;
* ``tests_passed`` comme **garde dure** (utilisée par le gate G2).

La **revue LLM** (``review_score``) est conservée à titre **informatif** (corps de
PR, relecteur humain) mais **n'entre pas** dans le composite gaté : un signal LLM
diff-scopé est non déterministe et asymétrique avant/après (la cause racine du
faux-rejet v9). Le « score du dashboard » du serveur (latence/coût de SES experts)
ne convient pas non plus : on mesure la qualité du **projet généré**.

**Frontière hôte (vague 1)** : le workspace mesuré est écrit par l'agent et par les
tests. Toute opération HÔTE dessus part d'un nom de fichier non fiable et reste
confinée (jamais de traversée, de lien symbolique suivi ni de réécriture hors
workspace — :mod:`collegue.sandbox.paths`) ; l'audit de dépendances, qui
résout/installe des dépendances choisies par l'agent, ne tourne JAMAIS sur l'hôte :
il passe par le sandbox fourni à :func:`measure`, sinon la mesure est refusée
(fail-closed) — jamais « 0 vulnérabilité ».

Module **isolé** : non câblé au runtime (la boucle G4 l'orchestre).
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.sandbox.paths import workspace_file

# Commande de couverture par défaut. ``python -m pytest`` (et non ``pytest`` nu) met le
# CWD sur ``sys.path`` → un projet src-layout (imports ``from app…``) est collectable sans
# install editable (#577) ; cohérent avec le gate de build qui utilise déjà ``python -m``.
DEFAULT_COVERAGE_COMMAND = "python -m pytest -q --cov --cov-report=term-missing"

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

# Regex de la ligne récapitulative TOTAL de pytest-cov / coverage.py : « TOTAL  120  6  95% ».
# Modèle COLONNES (et non « .*? » paresseux) : TOTAL puis des colonnes numériques, puis le %.
# (1) exige un espace après TOTAL → rejette un fichier nommé « TOTAL.py » ; (2) quantificateurs
# POSSESSIFS (``\d++`` / ``*+``, Python 3.11+) → AUCUN backtracking : une ligne TOTAL suivie d'un
# long run de chiffres sans « % » (stdout hostile) ne provoque PAS de ReDoS (#577, revue sécu).
_TOTAL_RE = re.compile(r"^\s*TOTAL\s++(?:\d++(?:\.\d++)?\s++)*+(\d++(?:\.\d++)?)%", re.MULTILINE)

# #577 : parseurs de couverture multi-écosystèmes, essayés du PLUS SPÉCIFIQUE au plus
# générique (1er match gagnant). Restaure le terme couverture du composite quand
# ``GATE_TEST_COMMAND`` émet un rapport NON pytest-cov (go / JS-TS / lcov / cobertura) —
# sans ça, couverture non mesurée → terme dominant figé. Chaque entrée = (motif, échelle) :
# 100.0 pour les RATIOS 0-1 (cobertura), 1.0 pour les % directs. Tous les motifs sont
# ANCRÉS (ligne de statut / libellé / balise) et LINÉAIRES (possessifs, pas de « .*? » non
# borné) → aucun « % » parasite capté, aucun backtracking catastrophique sur entrée hostile.
# parse_coverage borne le résultat à [0,100] (un faux-match ne peut pas fausser le composite).
# Limite go : « go test -cover ./... » émet une ligne PAR paquet (pas d'agrégat) → on lit la
# 1re ; pour un total fiable, préférer « go tool cover -func » (motif ``total:``).
_COVERAGE_PATTERNS = (
    (_TOTAL_RE, 1.0),  # pytest-cov / coverage.py « coverage report » (ligne TOTAL)
    (re.compile(r"^total:\s+(?:\([^)\n]*\)\s++)?(\d++(?:\.\d++)?)%", re.MULTILINE), 1.0),  # go tool cover -func
    (
        re.compile(r"^(?:ok|FAIL)\b[^\n]*\bcoverage:\s*+(\d++(?:\.\d++)?)%\s++of statements", re.MULTILINE),
        1.0,
    ),  # go test -cover (ancré sur la ligne de statut ok/FAIL)
    (re.compile(r"^\s*Statements\s*:\s*(\d++(?:\.\d++)?)\s*%", re.MULTILINE), 1.0),  # istanbul/jest text-summary
    (re.compile(r"^\s*All files\s*\|\s*(\d++(?:\.\d++)?)\s*\|", re.MULTILINE), 1.0),  # istanbul/vitest tableau
    (re.compile(r"^\s*lines\.*:\s*(\d++(?:\.\d++)?)%", re.IGNORECASE | re.MULTILINE), 1.0),  # lcov --summary
    (re.compile(r'<coverage[^>]*\bline-rate="(\d*+\.\d++|\d++)"'), 100.0),  # cobertura xml (ratio → %)
)


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
    dep_vulns: float = 0.5  # pénalité par vuln. de dépendance (signal opt-in, #551)


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
    # INFORMATIF (hors-gate, #551) : couverture de docstrings des symboles publics
    # [0–1], proxy de maintenabilité/documentation. N'entre PAS dans le composite.
    doc_coverage: float = 1.0
    # Vulnérabilités de dépendances (pip-audit). Signal OPT-IN : 0 par défaut (flag
    # off) → terme composite nul + règle gate no-op. Gaté (tolérance 0) quand activé.
    dep_vulns: int = 0
    # False si l'audit de dépendances était ACTIVÉ mais n'a pas pu être mené (outil absent,
    # sandbox indisponible, échec/timeout, sortie invalide, dépendance non auditée) : le
    # composite vaut alors -inf (mesure non fiable, rejet par le gate) — jamais 0 vuln.
    dep_audit_measured: bool = True


def parse_coverage(output: str) -> Optional[float]:
    """Extrait le % de couverture TOTAL de la sortie d'une commande de test.

    Essaie plusieurs formats (#577) du plus spécifique au plus générique — pytest-cov /
    coverage.py, go (``-func`` / ``-cover``), JS-TS istanbul/jest/vitest, lcov, cobertura
    XML — et renvoie le 1er match (les ratios cobertura 0-1 sont convertis en %, décimaux
    gérés). Retourne ``None`` si aucun format reconnu (sortie tronquée, pas de couverture…).
    """
    if not output:
        return None
    for pattern, scale in _COVERAGE_PATTERNS:
        match = pattern.search(output)
        if match:
            # Borne [0,100] (revue #577) : un faux-match ne peut pas injecter une valeur
            # aberrante qui dominerait le composite (poids couverture = 1.0) ou casserait
            # l'invariant ProjectQualityMetrics.coverage_pct « 0-100 ».
            return max(0.0, min(100.0, float(match.group(1)) * scale))
    return None


def composite_score(
    coverage_pct: float,
    security_weighted: float,
    *,
    lint_violations: int = 0,
    complexity_bad_blocks: int = 0,
    dep_vulns: int = 0,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
) -> float:
    """Score composite pondéré (déterministe).

    Monotone : ↑couverture → ↑ ; ↑sécu pondérée / ↑lint / ↑complexité / ↑vulns deps
    → ↓. La couverture (0–100) est normalisée en 0–1 ; les autres sont des pénalités.
    ``dep_vulns`` vaut 0 quand le flag est off (terme nul). La revue LLM et la
    couverture de docstrings n'y figurent pas (informatives, hors-gate).
    """
    return (
        weights.coverage * (coverage_pct / 100.0)
        - weights.security * security_weighted
        - weights.lint * lint_violations
        - weights.complexity * complexity_bad_blocks
        - weights.dep_vulns * dep_vulns
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
    # Les noms viennent du diff d'un agent (non fiable) et ruff RÉÉCRIT ces fichiers sur
    # l'hôte : traversée, chemin absolu hors workspace et lien symbolique (même interne)
    # sont refusés — on ne réécrit jamais à travers un lien ni hors du workspace.
    py = []
    for name in files:
        if not isinstance(name, str) or not name.endswith(".py"):
            continue
        confined = workspace_file(workspace, name, follow_internal_links=False)
        if confined is not None and confined not in py:
            py.append(confined)
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


# Dossiers élagués du calcul de couverture de docstrings (générés / tests).
_DOC_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        "tests",
        "test",
        "__tests__",
        "fixtures",
    }
)


def _is_doc_test_file(filename: str) -> bool:
    """Vrai pour un fichier de test (exclu de la couverture de docstrings)."""
    return filename == "conftest.py" or filename.startswith("test_") or filename.endswith("_test.py")


def _default_doc_coverage(workspace: str) -> float:
    """Fraction de symboles publics munis d'une docstring [0–1] (ast, stdlib).

    Proxy de **maintenabilité/documentation** déterministe : compte la docstring de
    module + celles des classes/fonctions publiques (nom ne commençant pas par ``_``)
    sur les ``.py`` du workspace, hors emplacements de test. ``1.0`` si aucun symbole
    public (vacuité). **Informatif** (hors-gate) ; générique Python, sans dépendance.
    """
    documented = total = 0
    for root, dirs, fnames in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _DOC_SKIP_DIRS]
        for fname in fnames:
            if not fname.endswith(".py") or _is_doc_test_file(fname):
                continue
            confined = workspace_file(workspace, os.path.join(root, fname), follow_internal_links=False)
            if confined is None:  # lien symbolique / hors workspace : jamais lu
                continue
            try:
                with open(confined, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
            except (OSError, SyntaxError, ValueError):
                continue
            total += 1  # le module lui-même
            if ast.get_docstring(tree):
                documented += 1
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith(
                    "_"
                ):
                    total += 1
                    if ast.get_docstring(node):
                        documented += 1
    return 1.0 if total == 0 else documented / total


# Audit de dépendances, exécuté DANS le sandbox de ``measure`` (jamais sur l'hôte).
# ``--no-deps --disable-pip`` : pip-audit lit ``requirements.txt`` lui-même — aucun pip,
# aucun résolveur, aucune construction de sdist, aucun clone VCS ; une exigence VCS/URL/
# locale/non épinglée fait échouer l'audit (donc refuse la mesure) au lieu d'être exécutée.
DEP_AUDIT_COMMAND = "pip-audit -r requirements.txt --no-deps --disable-pip --progress-spinner off --timeout 60 -f json"


class DepAuditUnavailable(RuntimeError):
    """L'audit de dépendances n'a pas pu être mené : la mesure doit être refusée (fail-closed)."""


def _count_audit_vulns(stdout: str) -> int:
    """Nombre de vulnérabilités d'une sortie ``pip-audit -f json`` STRICTEMENT valide.

    Toute forme inattendue (JSON invalide/tronqué, pas de liste ``dependencies``, entrée sans
    liste ``vulns``, dépendance ignorée par pip-audit ``skip_reason`` donc NON auditée)
    lève :class:`DepAuditUnavailable` : on ne convertit jamais un résultat douteux en « 0 ».
    """
    try:
        data = json.loads(stdout or "")
    except (TypeError, ValueError) as exc:
        raise DepAuditUnavailable("sortie pip-audit non JSON (échec, outil absent ou sortie tronquée)") from exc
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        raise DepAuditUnavailable("sortie pip-audit sans liste « dependencies »")
    total = 0
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("vulns"), list) or dep.get("skip_reason"):
            raise DepAuditUnavailable("dépendance non auditée par pip-audit (résultat incomplet)")
        total += len(dep["vulns"])
    return total


def _default_dep_audit(workspace: str, *, sandbox=None) -> int:
    """Compte les vulnérabilités connues des dépendances via pip-audit, EN SANDBOX (#551).

    Opt-in (appelé seulement si ``dep_vulns_enabled``). Audite ``requirements.txt`` du
    workspace (réseau vers la base de vulnérabilités : celui du sandbox). Aucun outil de
    résolution/installation ne tourne sur l'hôte. **Fail-closed** : sandbox absent, outil
    absent du sandbox (exit 127), échec/timeout, sortie invalide ou incomplète lèvent
    :class:`DepAuditUnavailable` — jamais un faux 0. Seul un ``requirements.txt`` absent
    (rien de déclaré à auditer) vaut 0.
    """
    if not os.path.lexists(os.path.join(workspace, "requirements.txt")):
        return 0
    run = getattr(sandbox, "run_tests", None)
    if run is None:
        raise DepAuditUnavailable("aucune isolation disponible pour l'audit de dépendances (sandbox absent)")
    try:
        result = run(workspace, DEP_AUDIT_COMMAND)
    except Exception as exc:  # noqa: BLE001 — sandbox indisponible ⇒ mesure refusée
        raise DepAuditUnavailable(f"sandbox indisponible pour l'audit de dépendances: {exc}") from exc
    if result is None or getattr(result, "timed_out", False):
        raise DepAuditUnavailable("audit de dépendances sans résultat ou interrompu par le délai")
    # pip-audit sort 0 (rien) ou 1 (vulnérabilités trouvées) avec un JSON valide ; tout
    # autre code (127 outil absent, 2 usage, 124 délai…) est un échec, même si stdout parle.
    if getattr(result, "exit_code", None) not in (0, 1):
        raise DepAuditUnavailable(f"pip-audit indisponible ou en échec (code {getattr(result, 'exit_code', None)})")
    return _count_audit_vulns(str(getattr(result, "stdout", "") or ""))


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
    doc_coverage_fn=None,
    dep_vulns_enabled: bool = False,
    dep_vulns_fn=None,
) -> ProjectQualityMetrics:
    """Mesure couverture + sécu + lint/complexité (déterministes) → composite.

    ``sandbox`` est injectable (mocké en CI) ; la couverture vient du parsing de la
    sortie de tests (multi-format, ``parse_coverage`` #577) et ``tests_passed`` = exit 0.
    Sécu (``security_scan_fn``) et
    lint/complexité (``quality_scan_fn``) viennent de scans **statiques déterministes**
    du **workspace** (injectables en tests). La couverture de docstrings
    (``doc_coverage_fn``) est **informative** (hors composite). Les vulns de dépendances
    (``dep_vulns_fn``) ne sont mesurées que si ``dep_vulns_enabled`` (opt-in, gaté) ; par défaut
    l'audit tourne dans ``sandbox`` et une panne refuse la mesure (composite -inf), jamais 0.
    La revue LLM (``reviewer``/``diff``) est **optionnelle et informative**.
    """
    test_res = sandbox.run_tests(workspace, coverage_command)
    tests_passed = bool(test_res.ok)
    raw_coverage = parse_coverage(test_res.stdout)
    coverage_measured = raw_coverage is not None
    coverage_pct = raw_coverage if coverage_measured else 0.0  # composite conservateur

    security_findings, security_weighted = _scan_security(workspace, scan_fn=security_scan_fn)
    lint_violations, complexity_bad_blocks, quality_measured = _scan_quality(workspace, scan_fn=quality_scan_fn)

    # Maintenabilité informative (hors-gate) : couverture de docstrings, jamais bloquante.
    try:
        doc_coverage = float((doc_coverage_fn or _default_doc_coverage)(workspace))
    except Exception:  # noqa: BLE001 — informatif, jamais bloquant
        doc_coverage = 1.0

    # Vulns de dépendances : OPT-IN (flag). Off ⇒ 0 (terme composite nul, gate no-op).
    # Symétrique à doc_coverage : une fn injectée qui lève ne fait pas planter la mesure.
    # ACTIVÉ mais indisponible (échec, timeout, outil absent, fn injectée qui lève) ⇒ mesure
    # REFUSÉE (fail-closed) : composite -inf, jamais « 0 vulnérabilité » qui améliorerait le score.
    dep_vulns = 0
    dep_audit_measured = True
    if dep_vulns_enabled:
        try:
            if dep_vulns_fn is not None:
                dep_vulns = int(dep_vulns_fn(workspace))
            else:
                dep_vulns = _default_dep_audit(workspace, sandbox=sandbox)
            if dep_vulns < 0:
                raise DepAuditUnavailable("nombre de vulnérabilités négatif")
        except Exception:  # noqa: BLE001 — toute panne d'audit ⇒ mesure non fiable (fail-closed)
            dep_vulns = -1
            dep_audit_measured = False

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
        composite=(
            composite_score(
                coverage_pct,
                security_weighted,
                lint_violations=lint_violations,
                complexity_bad_blocks=complexity_bad_blocks,
                dep_vulns=dep_vulns,
                weights=weights,
            )
            if dep_audit_measured
            else -math.inf
        ),
        coverage_measured=coverage_measured,
        review_score=review_score,
        lint_violations=lint_violations,
        complexity_bad_blocks=complexity_bad_blocks,
        quality_measured=quality_measured,
        doc_coverage=doc_coverage,
        dep_vulns=dep_vulns,
        dep_audit_measured=dep_audit_measured,
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
    manager.add_metric(project_id, "doc_coverage", metrics.doc_coverage)
    manager.add_metric(project_id, "dep_vulns", float(metrics.dep_vulns))
    manager.add_metric(project_id, "composite", metrics.composite)
