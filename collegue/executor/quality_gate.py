"""Gate qualité d'un diff produit par l'agent (E3, epic #362).

Deux vérifications, **fail-closed** :

1. **Tests** dans le :class:`~collegue.sandbox.executor.DockerSandbox` (C8) sur
   l'arbre patché — du code non fiable ne tourne donc jamais sur l'hôte. Les
   dépendances déclarées du projet (``requirements.txt`` / ``pyproject.toml``)
   sont installées dans le **même** conteneur avant ``pytest`` (#414).
2. **Revue experte** via l'outil ``code_review`` existant (rôle LLM ``REVIEWER``),
   derrière un :class:`Reviewer` injectable (mocké en CI, réel en ``integration``).

``passed`` n'est vrai que si les tests passent **et** la revue ne bloque pas
**et** aucune erreur n'est survenue. Toute incertitude (tests non exécutables,
exception du reviewer) ⇒ ``passed=False`` : on ne laisse jamais un doute valider
le diff. Les ``BaseException`` (ex. ``BudgetExceeded``, C4) ne sont **pas**
avalées — elles remontent.

:meth:`QualityReport.to_markdown` produit le rapport pour le corps de PR (E4) ;
le texte de revue (potentiellement non fiable) est **inline-isé puis fencé** pour
qu'il ne puisse pas forger de fausse bannière/section (cf. P5).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from typing import Iterator, List, Optional, Protocol, Tuple, runtime_checkable

from collegue.executor.agent import IssueSpec
from collegue.sandbox.executor import DockerSandbox
from collegue.textnorm import inline

# `python -m pytest` (et non le script `pytest`) ajoute le répertoire de travail
# (la racine du workspace, montée sur `/workspace`) à `sys.path`. Sans ça, les tests
# d'un projet en layout `src/`/`app/` qui importent par package (`from app.x import …`,
# `from src.x import …`) lèvent `ModuleNotFoundError` à la collecte → le gate échoue
# à tort (tests verts vus comme rouges). Voir issue #413.
DEFAULT_TEST_COMMAND = "python -m pytest -q"
# Note visible dans la sortie du gate quand l'installation des deps échoue (#414).
_INSTALL_FAILED_NOTE = "[gate] installation des dépendances en échec — tests lancés quand même (#414)"


def deps_install_prelude(workspace: str, *, strict: bool = False) -> Optional[str]:
    """Préambule shell installant les dépendances du projet avant les tests (#414).

    Le conteneur de tests est **éphémère** et distinct de celui où l'agent a
    travaillé : sans installation, tout projet à dépendance tierce échoue en
    ``ModuleNotFoundError`` (environnement incomplet, pas code faux) et le gate
    refuse la PR à tort (cas réel FacNor : ``No module named 'jose'``).

    Contraintes de conception :
    - install et tests partagent le **même** conteneur : ``pip install --user``
      écrit sous ``HOME=/tmp`` (tmpfs compatible rootfs read-only) qui **meurt avec
      le conteneur** → on PRÉFIXE la commande de tests au lieu d'un run séparé ;
    - échec d'install **toléré** par défaut (``|| echo``) : les tests tournent
      quand même (comportement historique, ex. sandbox sans réseau) et la cause
      reste visible dans la sortie du gate — détectée et remontée dans
      ``QualityReport.deps_install_failed`` (#439) ;
    - ``strict=True`` (#439) : l'échec d'installation devient BLOQUANT (``&&``) —
      l'installabilité des dépendances déclarées fait partie du contrat ;
    - ``None`` si le workspace ne déclare aucune dépendance installable.
    """
    parts: List[str] = []
    if os.path.isfile(os.path.join(workspace, "requirements.txt")):
        parts.append("python -m pip install --user --no-cache-dir -q -r requirements.txt")
    if os.path.isfile(os.path.join(workspace, "pyproject.toml")) or os.path.isfile(os.path.join(workspace, "setup.py")):
        parts.append("python -m pip install --user --no-cache-dir -q -e .")
    if not parts:
        return None
    if strict:
        return " && ".join(f"({part})" for part in parts)
    return "; ".join(f"({part} || echo '{_INSTALL_FAILED_NOTE}')" for part in parts)


# Bannière de la passe d'installabilité dans la sortie du gate (#439).
_INSTALLABILITY_BANNER = "[gate] installabilité : venv nu + requirements + collecte (#439)"
_GATE_VENV = "/tmp/.gate_venv"

# Passe smoke-run (#458) : port d'écoute imposé dans le conteneur du gate.
_SMOKE_BANNER = "[gate] smoke run : l'app démarre et répond (#458)"
_SMOKE_PORT = 8765
_SMOKE_HEREDOC = "COLLEGUE_SMOKE_458"

# Entrées FastAPI usuelles : (chemin relatif, cible uvicorn). Détection
# délibérément conservatrice — sans entrée reconnue, la passe est SKIPPÉE
# (pas de faux rouge sur un projet non-web), sauf commande explicite.
_ASGI_APP_CANDIDATES = (
    ("main.py", "main:app"),
    ("app.py", "app:app"),
    (os.path.join("app", "main.py"), "app.main:app"),
    (os.path.join("src", "main.py"), "src.main:app"),
)

# Sonde exécutée DANS le conteneur du gate : démarre le serveur, attend qu'il
# réponde, vérifie chaque chemin (<500 = vivant ; 4xx toléré — route protégée
# ou méthode — l'app A répondu), imprime la queue du log serveur en cas d'échec.
# %%-formaté (pas f-string) : le code python embarqué garde ses accolades.
_SMOKE_PROBE_TEMPLATE = """\
import subprocess, sys, time, urllib.error, urllib.request

command = %(command)r
paths = %(paths)r
timeout = %(timeout)r
log = open("/tmp/.collegue_smoke.log", "w+", encoding="utf-8", errors="replace")
proc = subprocess.Popen(command, shell=True, stdout=log, stderr=subprocess.STDOUT)
base = "http://127.0.0.1:%(port)d"
deadline = time.time() + timeout
verdicts = []
for path in paths:
    status = None
    attempted = False
    # Au moins UNE tentative par chemin, même si le précédent a épuisé le délai
    # (sinon : faux rouge « sans réponse » sur un chemin jamais contacté).
    while not attempted or time.time() < deadline:
        attempted = True
        if proc.poll() is not None:
            break  # le serveur est mort avant de répondre
        try:
            status = urllib.request.urlopen(base + path, timeout=2).status
            break
        except urllib.error.HTTPError as exc:
            status = exc.code
            break
        except Exception:
            time.sleep(0.5)
    verdicts.append((path, status))
time.sleep(0.3)  # resserre la fenêtre « répond une fois puis meurt »
ok = proc.poll() is None and all(s is not None and s < 500 for _p, s in verdicts)
for path, status in verdicts:
    print("[gate] smoke run", path, "->", status if status is not None else "sans réponse")
if proc.poll() is not None:
    print(
        "[gate] smoke run : le processus serveur s'est terminé (code", str(proc.poll()) + ")",
        "— la commande de démarrage doit rester au premier plan",
    )
if not ok:
    log.flush()
    log.seek(0)
    print("[gate] smoke run ÉCHEC — queue du log serveur :")
    print(log.read()[-4000:])
proc.terminate()
sys.exit(0 if ok else 1)
"""


def _detect_asgi_app(workspace: str) -> Optional[str]:
    """Cible uvicorn (``module:app``) si le workspace expose une app FastAPI (#458)."""
    for rel, target in _ASGI_APP_CANDIDATES:
        path = os.path.join(workspace, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                # Lecture bornée : fichier non fiable (écrit par l'agent), et
                # « FastAPI( » vit toujours en tête de module.
                source = handle.read(65536)
        except OSError:
            continue
        if "FastAPI(" in source:
            return target
    return None


def _smoke_probe_script(command: str, paths: Tuple[str, ...], timeout: float, *, port: int = _SMOKE_PORT) -> str:
    """Source python de la sonde smoke-run (pur — testable par ``compile``)."""
    # `/` de tête normalisé : sans lui, urlopen("…:8765health") lèverait à
    # chaque tentative et brûlerait tout le délai en silence.
    normalized = tuple(path if path.startswith("/") else "/" + path for path in paths) or ("/",)
    return _SMOKE_PROBE_TEMPLATE % {
        "command": command,
        "paths": normalized,
        "timeout": float(timeout),
        "port": int(port),
    }


def smoke_run_command(
    workspace: str,
    *,
    command: Optional[str] = None,
    paths: Tuple[str, ...] = ("/",),
    timeout: float = 30.0,
) -> Optional[str]:
    """Commande de la passe smoke-run (#458). Fail-closed une fois déclenchée.

    Le gate validait le livrable par pytest + revue sans JAMAIS lancer
    l'application : toute divergence entre l'init des tests et celui de la prod
    passait sous le radar (FacNor v3 : 34 tests verts via ``create_all``, mais
    ``schema.sql`` incomplet → flux central en 500 sur installation fraîche,
    tous gates verts — pour la 2e campagne consécutive).

    - ``command`` : commande de démarrage explicite — elle doit écouter sur
      ``127.0.0.1:8765`` et **rester au premier plan** (un wrapper qui
      daemonise est vu comme « serveur mort ») ; sinon auto-détection FastAPI
      (cf. :data:`_ASGI_APP_CANDIDATES`) → ``python -m uvicorn`` ;
    - ``paths`` : chemins sondés — chacun doit répondre **< 500** (4xx toléré :
      l'app a répondu ; 5xx ou silence = rouge) ;
    - ``timeout`` : budget total d'attente de réponse (secondes) — à garder
      sous le timeout du conteneur sandbox (120 s par défaut, partagé avec
      pip/pytest/npm) ;
    - ``None`` si aucune app détectable ET pas de commande explicite (projet
      non-web : la passe ne s'applique pas).

    S'exécute dans le **même** conteneur que les autres passes (les deps du
    projet y sont déjà installées, #414) ; tout meurt avec le conteneur.
    """
    if not command:
        target = _detect_asgi_app(workspace)
        if target is None:
            return None
        command = f"python -m uvicorn {target} --host 127.0.0.1 --port {_SMOKE_PORT}"
    script = _smoke_probe_script(command, paths, timeout)
    return f"python - <<'{_SMOKE_HEREDOC}'\n{script}{_SMOKE_HEREDOC}"


def installability_command(workspace: str) -> Optional[str]:
    """Passe d'installabilité en environnement NU (#439). Fail-closed.

    L'image sandbox pré-installe une stack web pour servir l'**agent** (#414) :
    une dépendance manquante ou mal pinnée de ``requirements.txt`` y est
    invisible — gate vert sur un projet **non installable** ailleurs (FacNor v2 :
    ``email-validator``/``httpx``/``python-multipart`` absents, ``pytest`` EXIT 4
    en environnement propre). Cette passe valide le **contrat d'installation** du
    livrable : venv vierge → ``pip install -r requirements.txt`` → collecte
    pytest (exécute les imports de conftest/tests, exactement le mode d'échec
    constaté). ``None`` sans ``requirements.txt``. Nécessite le réseau (PyPI) —
    opt-in via ``check_installability``.
    """
    if not os.path.isfile(os.path.join(workspace, "requirements.txt")):
        return None
    # --retries/--timeout EXPLICITES (#461) : une micro-coupure PyPI pendant la
    # passe coûtait une tentative fonctionnelle entière (échec terminal FacNor
    # v3, itération 14) — pip ré-essaie d'abord, le moteur ne décompte qu'après.
    # --timeout 30 (pas plus) : le conteneur du gate a un budget TOTAL de
    # 120 s — un timeout socket long plus un retry le dépasserait (kill du
    # conteneur, le pire diagnostic possible).
    pip_flags = "--no-cache-dir -q --retries 5 --timeout 30"
    return (
        f"python -m venv --clear {_GATE_VENV}"
        f" && {_GATE_VENV}/bin/python -m pip install {pip_flags} -r requirements.txt"
        f" && {_GATE_VENV}/bin/python -m pip install {pip_flags} pytest"
        f" && {_GATE_VENV}/bin/python -m pytest --collect-only -q"
    )


# Bannière insérée entre la passe Python et la passe frontend dans la sortie du gate.
_FRONTEND_BANNER = "[gate] frontend : install + build + tests (#438)"


def _real_test_script(scripts: dict) -> bool:
    """Vrai si ``package.json`` déclare un script ``test`` RÉEL (pas le stub npm).

    Le stub par défaut de ``npm init`` (« Error: no test specified… && exit 1 »)
    ferait échouer le gate de TOUT projet front sans tests — on ne lance que les
    vrais scripts.
    """
    script = str(scripts.get("test") or "")
    return bool(script.strip()) and "no test specified" not in script


def _frontend_dirs(workspace: str) -> List[str]:
    """Répertoires (relatifs) candidats à la passe frontend (#457).

    La racine d'abord (comportement historique #438), puis les sous-répertoires
    de **premier niveau** contenant un ``package.json``. Le layout CIBLE du moteur
    pour les projets web (backend Python à la racine + ``frontend/``) place le
    ``package.json`` dans un sous-répertoire : ne sonder que la racine rendait la
    passe silencieusement absente — main mergé avec 42 erreurs TypeScript, tout
    gate vert (run FacNor v3). ``node_modules`` et les répertoires cachés sont
    ignorés ; tri déterministe.
    """
    dirs: List[str] = []
    if os.path.isfile(os.path.join(workspace, "package.json")):
        dirs.append(".")
    try:
        entries = sorted(os.listdir(workspace))
    except OSError:
        return dirs
    for entry in entries:
        if entry.startswith(".") or entry == "node_modules":
            continue
        if os.path.isfile(os.path.join(workspace, entry, "package.json")):
            dirs.append(entry)
    return dirs


def frontend_gate_command(workspace: str, subdir: str = ".") -> Optional[str]:
    """Commande de gate frontend pour le ``package.json`` de ``workspace/subdir`` (#438).

    Le gate historique ne lançait QUE pytest : compilation TypeScript, build Vite
    et tests front n'étaient JAMAIS validés — des PRs front cassées au type-check
    sont mergées en série avec un verdict « ✅ PASSÉ » (main FacNor : ``npm run
    build`` rouge, 11 erreurs TS, toutes PRs gate vert).

    **Fail-closed, comme pytest** : npm absent de l'image, install, build ou tests
    front en échec ⇒ gate rouge. Enchaîné dans le **même** conteneur :

    - install : ``npm ci`` (reproductible), repli ``npm install`` (pas de lockfile) ;
    - build : script ``build`` s'il existe, sinon ``tsc --noEmit`` quand TypeScript
      est déclaré (tsconfig.json + dépendance ``typescript`` — installée juste
      avant, d'où ``--no-install``) ;
    - tests front : script ``test`` réel uniquement (stub npm ignoré).

    ``subdir`` (#457) : répertoire relatif du front dans le workspace (monorepo
    backend + ``frontend/``) — la commande s'exécute dedans (``cd``). ``CI=true``
    neutralise les modes watch (react-scripts, vitest) ; le cache npm vit sous
    ``/tmp`` (rootfs read-only, #414). ``None`` si pas de ``package.json``.
    """
    base = workspace if subdir == "." else os.path.join(workspace, subdir)
    pkg_path = os.path.join(base, "package.json")
    if not os.path.isfile(pkg_path):
        return None
    scripts: dict = {}
    declared: set = set()
    try:
        with open(pkg_path, encoding="utf-8") as handle:
            data = json.load(handle) or {}
        scripts = dict(data.get("scripts") or {})
        declared = set(data.get("dependencies") or {}) | set(data.get("devDependencies") or {})
    except (OSError, ValueError):
        # package.json illisible : on garde au moins l'install (fail-closed — npm
        # signalera lui-même le JSON invalide au lieu d'un gate silencieusement vert).
        pass
    steps = ["(npm ci --no-audit --no-fund --silent || npm install --no-audit --no-fund --silent)"]
    if "build" in scripts:
        steps.append("npm run build --silent")
    elif "typescript" in declared and os.path.isfile(os.path.join(base, "tsconfig.json")):
        steps.append("npx --no-install tsc --noEmit")
    if _real_test_script(scripts):
        steps.append("npm test --silent")
    prefix = "export CI=true NPM_CONFIG_CACHE=/tmp/.npm; "
    if subdir != ".":
        # `--` : un nom de répertoire commençant par `-` (contenu non fiable,
        # écrit par l'agent) serait sinon consommé comme OPTION de `cd` — la
        # passe npm tournerait silencieusement dans $HOME au lieu du front.
        prefix += f"cd -- {shlex.quote(subdir)} && "
    return prefix + " && ".join(steps)


# Triple-backtick de remplacement : neutralise les fences pour qu'un texte non
# fiable ne puisse pas refermer le bloc de code et forger une fausse section.
_FENCE = "```"
_FENCE_SAFE = "ʼʼʼ"


def _fence_safe_line(text) -> str:
    """Une ligne sûre dans un bloc fencé : inline-isée + fences neutralisés."""
    return inline(text).replace(_FENCE, _FENCE_SAFE)


# En-dessous de ce score (cf. seuil interne de code_review), la revue bloque.
DEFAULT_MIN_QUALITY = 0.5
# Sévérités qui bloquent à elles seules, quel que soit le score. On inclut
# ``error`` (pas seulement ``critical``) : l'expert code_review émet ``error`` pour
# des problèmes sérieux (complexité élevée, motifs d'injection) et traite lui-même
# critical+error comme graves. Comme le score est normalisé par la taille, un seul
# ``error`` sur un gros diff donnerait un score ~0.96 et passerait sinon — le gate
# resterait laxiste. On reste donc fail-closed côté sévérité.
BLOCKING_SEVERITIES = frozenset({"critical", "error"})


@dataclass(frozen=True)
class ReviewFindingLite:
    """Finding de revue, découplé du modèle Pydantic de ``code_review``."""

    category: str
    severity: str
    title: str


@dataclass(frozen=True)
class ReviewOutcome:
    """Résultat normalisé d'une revue."""

    summary: str
    quality_score: float
    findings: Tuple[ReviewFindingLite, ...] = ()
    blocking: bool = False


@runtime_checkable
class Reviewer(Protocol):
    """Revue d'un diff. Async pour autoriser un reviewer LLM (rôle REVIEWER)."""

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome: ...


@dataclass(frozen=True)
class AdequacyOutcome:
    """Verdict d'adéquation diff↔issue (#437)."""

    implemented: bool
    justification: str = ""


@runtime_checkable
class AdequacyChecker(Protocol):
    """Contrôle « ce diff implémente-t-il l'issue ? » (#437). Async (LLM possible)."""

    async def check(self, diff: str, issue: IssueSpec, ctx) -> AdequacyOutcome: ...


@dataclass
class QualityReport:
    """Verdict combiné tests + revue d'un diff."""

    tests_passed: bool
    test_exit_code: int
    test_output: str
    review_summary: str
    review_findings: Tuple[ReviewFindingLite, ...]
    review_blocking: bool
    passed: bool
    review_error: Optional[str] = None
    # #439 : l'installation des dépendances déclarées a échoué pendant le gate
    # (mode toléré) — le vert des tests peut venir des paquets de l'IMAGE, pas du
    # requirements.txt du projet. Signal fort pour la PR et l'audit.
    deps_install_failed: bool = False
    # #437 : contrôle d'adéquation diff↔issue (None = non évalué). Fail-closed :
    # implemented=False ou erreur du checker ⇒ passed=False.
    adequacy_implemented: Optional[bool] = None
    adequacy_justification: str = ""
    adequacy_error: Optional[str] = None
    # #437 : le diff touche-t-il au moins un fichier de test ? (signal — défaut
    # True pour ne pas générer d'avertissement sur les rapports construits sans
    # cette information).
    tests_touched: bool = True

    def to_markdown(self) -> str:
        """Rapport Markdown pour le corps de PR (texte de revue fencé, anti-injection)."""
        tests_badge = "✅ réussis" if self.tests_passed else "❌ échec"
        lines = [
            "## Gate qualité",
            "",
            f"**Tests** : {tests_badge} (code de sortie {self.test_exit_code})",
        ]
        if self.deps_install_failed:
            lines.append(
                "> ⚠️ **installation des dépendances déclarées EN ÉCHEC** pendant le gate (#439) — "
                "le vert peut venir des paquets pré-installés de l'image sandbox, pas du "
                "`requirements.txt` du projet (installabilité non prouvée)."
            )
        lines += [
            "",
            "<details><summary>Sortie des tests</summary>",
            "",
            "```text",
            # multi-ligne préservé ; on neutralise seulement le délimiteur de fence.
            self.test_output.replace(_FENCE, _FENCE_SAFE) or "(vide)",
            "```",
            "",
            "</details>",
            "",
            f"**Revue experte** : {'⛔ bloquante' if self.review_blocking else '✅ non bloquante'}",
        ]
        if self.review_error:
            lines.append(f"> ⚠️ revue indisponible : {_fence_safe_line(self.review_error)}")
        lines += ["", "```text", _fence_safe_line(self.review_summary) or "(pas de résumé)"]
        for finding in self.review_findings:
            lines.append(
                "- "
                f"[{_fence_safe_line(finding.severity)}] "
                f"{_fence_safe_line(finding.category)} : {_fence_safe_line(finding.title)}"
            )
        lines += ["```"]
        if self.adequacy_implemented is not None or self.adequacy_error:
            if self.adequacy_error:
                badge = "⚠️ indisponible (fail-closed)"
                detail = self.adequacy_error
            else:
                badge = "✅ conforme" if self.adequacy_implemented else "⛔ NON conforme"
                detail = self.adequacy_justification
            lines += ["", f"**Adéquation à l'issue (#437)** : {badge}"]
            if detail:
                lines.append(f"> {_fence_safe_line(detail)}")
        if not self.tests_touched:
            lines += [
                "",
                "> ⚠️ **aucun fichier de test touché** par ce diff (#437) — la feature livrée "
                "n'est couverte par aucun test nouveau ou modifié.",
            ]
        lines += ["", f"**Verdict** : {'✅ PASSÉ' if self.passed else '❌ NON PASSÉ'}"]
        return "\n".join(lines)


# Indices d'un livrable NON-code dans le titre d'une issue (#437). Fail-closed :
# sans indice explicite, une issue est réputée attendre du CODE.
_NON_CODE_HINTS = (
    "doc",
    "readme",
    "config",
    "données",
    "data",
    "seed",
    "dépendance",
    "dependance",
    "changelog",
    "licence",
    "license",
)


def issue_expects_code(issue: IssueSpec) -> bool:
    """Heuristique fail-closed : cette issue attend-elle une implémentation CODE ? (#437)

    ``True`` par défaut ; ``False`` seulement quand le titre annonce explicitement
    un livrable non-code (documentation, configuration, données…). C'est ce qui
    distingue un « diff data-only attendu » d'une **livraison fantôme** (feature
    fermée par +1 ligne de requirements — cas réel FacNor #69/export PDF).
    """
    title = (issue.title or "").lower()
    return not any(hint in title for hint in _NON_CODE_HINTS)


# Fichiers de test : tests/, __tests__/, test_x.py, x_test.go, x.test.ts, x.spec.ts…
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$|[^/]+[._]test\.[a-z]+$|[^/]+\.spec\.[a-z]+$")


def tests_touched(diff: str) -> bool:
    """Le diff touche-t-il au moins un fichier de test ? (#437, signal de couverture)."""
    return any(_TEST_PATH_RE.search(path.lower()) for path in _diff_paths(diff))


_ADEQUACY_SYSTEM = (
    "Tu es un relecteur d'ADÉQUATION (rôle REVIEWER). On te donne une issue (titre, "
    "critères d'acceptation) et le diff livré pour la fermer. Tu ne juges PAS le style : "
    "uniquement si le diff RÉALISE concrètement ce que l'issue demande. "
    'Réponds STRICTEMENT en JSON : {"implemented": true|false, "justification": "..."}. '
    "implemented=false si la feature est absente ou hors-spec (ex. : une seule ligne de "
    "dépendance pour un service entier, un schéma sans la logique demandée)."
)


def _parse_adequacy(text: str) -> AdequacyOutcome:
    """Parsing tolérant de la réponse du LLM — **fail-closed** (illisible ⇒ non conforme)."""
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return AdequacyOutcome(
                implemented=bool(data.get("implemented")),
                justification=str(data.get("justification") or "")[:500],
            )
        except ValueError:
            pass
    if not raw:
        return AdequacyOutcome(False, "réponse vide du contrôle d'adéquation")
    return AdequacyOutcome(False, f"réponse illisible du contrôle d'adéquation : {raw[:300]}")


class LLMAdequacyChecker:
    """:class:`AdequacyChecker` par LLM (#437) — fail-closed.

    ``sample_fn`` : ``async (prompt, system_prompt) -> str``, injectable (mocké en
    CI) ; défaut = ``generate_text`` des providers LLM avec la config du serveur.
    Le diff est borné (``max_diff_chars``) pour rester dans la fenêtre du modèle.
    """

    def __init__(self, sample_fn=None, *, max_diff_chars: int = 20000):
        self._sample_fn = sample_fn or _default_adequacy_sample_fn()
        self._max_diff_chars = max_diff_chars

    async def check(self, diff: str, issue: IssueSpec, ctx) -> AdequacyOutcome:
        prompt = (
            f"## Issue à fermer\n{issue.to_prompt()}\n\n"
            f"## Diff livré\n```diff\n{(diff or '(diff vide)')[: self._max_diff_chars]}\n```\n\n"
            "Ce diff implémente-t-il concrètement l'issue ?"
        )
        text = await self._sample_fn(prompt, _ADEQUACY_SYSTEM)
        return _parse_adequacy(text)


def _default_adequacy_sample_fn():  # pragma: no cover - chemin réel (integration)
    from collegue.config import settings
    from collegue.resources.llm.providers import LLMConfig, generate_text

    config = LLMConfig(
        model_name=settings.llm_model,
        api_key=settings.llm_api_key,
        max_tokens=settings.MAX_TOKENS,
        temperature=0.2,  # verdict, pas créativité
    )

    async def sample(prompt: str, system_prompt: str) -> str:
        response = await generate_text(config, prompt, system_prompt)
        return response.text

    return sample


class FakeAdequacyChecker:
    """:class:`AdequacyChecker` déterministe pour la CI (aucun LLM)."""

    def __init__(
        self, *, implemented: bool = True, justification: str = "conforme", raises: Optional[Exception] = None
    ):
        self._implemented = implemented
        self._justification = justification
        self._raises = raises
        self.calls: List[int] = []

    async def check(self, diff: str, issue: IssueSpec, ctx) -> AdequacyOutcome:
        self.calls.append(issue.number)
        if self._raises is not None:
            raise self._raises
        return AdequacyOutcome(implemented=self._implemented, justification=self._justification)


class FakeReviewer:
    """:class:`Reviewer` déterministe pour la CI (aucun LLM)."""

    def __init__(
        self,
        *,
        summary: str = "revue simulée : RAS",
        quality_score: float = 0.9,
        findings: Optional[List[ReviewFindingLite]] = None,
        blocking: bool = False,
        raises: Optional[Exception] = None,
    ):
        self._summary = summary
        self._quality_score = quality_score
        self._findings = tuple(findings or ())
        self._blocking = blocking
        self._raises = raises

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome:
        if self._raises is not None:
            raise self._raises
        return ReviewOutcome(
            summary=self._summary,
            quality_score=self._quality_score,
            findings=self._findings,
            blocking=self._blocking,
        )


async def run_quality_gate(
    workspace: str,
    diff: str,
    ctx,
    *,
    sandbox: Optional[DockerSandbox] = None,
    reviewer: Optional[Reviewer] = None,
    issue: Optional[IssueSpec] = None,
    test_command: str = DEFAULT_TEST_COMMAND,
    install_deps: bool = True,
    frontend_gate: bool = True,
    require_deps_install: bool = False,
    check_installability: bool = False,
    adequacy_checker: Optional[AdequacyChecker] = None,
    require_test_changes: bool = False,
    smoke_run: bool = False,
    smoke_command: Optional[str] = None,
    smoke_paths: Tuple[str, ...] = ("/",),
    smoke_timeout: float = 30.0,
) -> QualityReport:
    """Exécute les tests (sandbox) + la revue (reviewer) sur un diff. Fail-closed.

    ``sandbox``/``reviewer`` sont injectables (mockés en CI). Tout échec ou
    indisponibilité ⇒ ``passed=False``. Les ``BaseException`` remontent.
    ``install_deps`` (défaut vrai) : préfixe la commande de tests par l'installation
    des dépendances déclarées du projet (cf. :func:`deps_install_prelude`, #414).
    ``frontend_gate`` (défaut vrai, #438) : pour chaque ``package.json`` détecté —
    à la racine OU dans un sous-répertoire de 1er niveau (#457, layout monorepo
    backend+``frontend/``) — enchaîne install + build/type-check + tests front
    dans le même conteneur, fail-closed (cf. :func:`frontend_gate_command`) —
    sans quoi « tests verts » signifie « tests *Python* verts », même sur un diff
    100 % frontend.
    ``require_deps_install`` (#439) : l'échec d'installation des dépendances
    déclarées devient BLOQUANT (au lieu d'être toléré et seulement signalé via
    ``QualityReport.deps_install_failed``).
    ``check_installability`` (#439) : ajoute la passe d'installabilité en venv NU
    (cf. :func:`installability_command`) — prouve que le livrable s'installe
    depuis SES requirements, pas depuis les paquets de l'image sandbox.
    ``adequacy_checker`` (#437) : contrôle « ce diff implémente-t-il l'issue ? »
    (LLM rôle REVIEWER, fail-closed), lancé seulement quand le reste du gate est
    vert ET qu'une ``issue`` est fournie — un diff trivial sans rapport avec les
    critères (« livraison fantôme ») ne passe plus.
    ``require_test_changes`` (#437) : exige qu'au moins un fichier de test soit
    touché par le diff (sinon, simple signal ``tests_touched`` dans le rapport).
    ``smoke_run`` (#458) : passe finale qui DÉMARRE l'application dans le même
    conteneur et vérifie qu'elle répond (cf. :func:`smoke_run_command`) — sans
    elle, une divergence d'init tests/prod livre un produit en 500 avec tous
    les gates verts. ``smoke_command`` : démarrage explicite (écoute sur
    127.0.0.1:8765) ; ``smoke_paths`` : chemins sondés (chacun doit répondre
    < 500). Skippée si aucune app n'est détectable et qu'aucune commande n'est
    fournie.
    """
    sandbox = sandbox or DockerSandbox()
    reviewer = reviewer or _default_reviewer()

    # 1. Tests dans le sandbox. Une incapacité à les exécuter = non passé
    #    (fail-closed), pas une exception qui remonterait.
    deps_install_failed = False
    try:
        command = test_command
        if install_deps:
            prelude = deps_install_prelude(workspace, strict=require_deps_install)
            if prelude is not None:
                # Strict (#439) : install bloquante. Toléré (#414) : les tests
                # tournent quand même, l'échec laisse sa note dans la sortie.
                command = f"({prelude}) && {test_command}" if require_deps_install else f"{prelude}; {test_command}"
        if frontend_gate:
            for front_dir in _frontend_dirs(workspace):
                front = frontend_gate_command(workspace, subdir=front_dir)
                if front is None:
                    continue
                # `&&` : chaque passe frontend ne tourne que si la précédente est
                # verte (le verdict est déjà rouge sinon) et son échec rend le
                # gate rouge. Une passe PAR répertoire détecté (#457 : la racine
                # ET les sous-répertoires de 1er niveau — layout monorepo).
                banner = _FRONTEND_BANNER if front_dir == "." else f"{_FRONTEND_BANNER} [{front_dir}]"
                command = f"({command}) && echo {shlex.quote(banner)} && ({front})"
        if check_installability:
            installability = installability_command(workspace)
            if installability is not None:
                command = f"({command}) && echo '{_INSTALLABILITY_BANNER}' && ({installability})"
        if smoke_run:
            smoke = smoke_run_command(workspace, command=smoke_command, paths=smoke_paths, timeout=smoke_timeout)
            if smoke is not None:
                # Dernière passe (le heredoc doit clore la commande) : l'app est
                # lancée dans le même conteneur, après install des deps (#414).
                command = f"({command}) && echo {shlex.quote(_SMOKE_BANNER)} && {smoke}"
        test_res = sandbox.run_tests(workspace, command)
        tests_passed = test_res.ok
        test_exit_code = test_res.exit_code
        test_output = "\n".join(part for part in (test_res.stdout, test_res.stderr) if part).strip()
        # #439 : la note du prelude toléré (#414) devient un SIGNAL structuré —
        # un vert obtenu avec une install en échec n'a pas la même valeur.
        deps_install_failed = _INSTALL_FAILED_NOTE in test_output
    except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
        tests_passed = False
        test_exit_code = -1
        test_output = f"tests non exécutables : {exc}"

    # 2. Revue. Une exception du reviewer = bloquant (fail-closed).
    review_error: Optional[str] = None
    try:
        outcome = await reviewer.review(diff, ctx, issue=issue)
        review_summary = outcome.summary
        review_findings = outcome.findings
        review_blocking = outcome.blocking
    except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
        review_error = str(exc) or repr(exc)
        review_summary = "revue indisponible (erreur)"
        review_findings = ()
        review_blocking = True

    # 3. Adéquation diff↔issue (#437) — lancée seulement si le reste est vert
    #    (économie d'appels LLM : un gate déjà rouge n'a pas besoin du verdict).
    #    Fail-closed : non conforme OU erreur du checker ⇒ non passé.
    would_pass = bool(tests_passed and not review_blocking and review_error is None)
    adequacy_implemented: Optional[bool] = None
    adequacy_justification = ""
    adequacy_error: Optional[str] = None
    if adequacy_checker is not None and issue is not None and would_pass:
        try:
            adequacy = await adequacy_checker.check(diff, issue, ctx)
            adequacy_implemented = bool(adequacy.implemented)
            adequacy_justification = adequacy.justification
        except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
            adequacy_error = str(exc) or repr(exc)

    touched = tests_touched(diff)
    passed = would_pass and adequacy_implemented is not False and adequacy_error is None
    if require_test_changes and not touched:
        passed = False
    return QualityReport(
        tests_passed=tests_passed,
        test_exit_code=test_exit_code,
        test_output=test_output,
        review_summary=review_summary,
        review_findings=review_findings,
        review_blocking=review_blocking,
        passed=passed,
        review_error=review_error,
        deps_install_failed=deps_install_failed,
        adequacy_implemented=adequacy_implemented,
        adequacy_justification=adequacy_justification,
        adequacy_error=adequacy_error,
        tests_touched=touched,
    )


def _default_reviewer() -> Reviewer:
    """Reviewer par défaut : l'expert ``code_review`` réel (rôle REVIEWER)."""
    return ExpertReviewer()


def outcome_from_review(response, *, min_quality: float = DEFAULT_MIN_QUALITY) -> ReviewOutcome:
    """Mappe une ``CodeReviewResponse`` vers un :class:`ReviewOutcome` (pur, testable).

    Bloquant si le score est sous le seuil **ou** s'il existe un finding de sévérité
    bloquante (``critical`` ou ``error``, cf. :data:`BLOCKING_SEVERITIES`).
    """
    findings = tuple(
        ReviewFindingLite(category=f.category, severity=f.severity, title=f.title) for f in response.findings
    )
    blocking = response.quality_score < min_quality or any(f.severity in BLOCKING_SEVERITIES for f in findings)
    return ReviewOutcome(
        summary=response.summary,
        quality_score=response.quality_score,
        findings=findings,
        blocking=blocking,
    )


# Extensions → langage supporté par l'outil ``code_review`` (python/js/ts/php).
_REVIEW_LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".php": "php",
}


def _diff_paths(diff: str) -> Iterator[str]:
    """Chemins (uniques) des fichiers touchés par un diff unifié git."""
    seen: set[str] = set()
    for line in (diff or "").splitlines():
        path = None
        if line.startswith("+++ b/"):
            path = line[len("+++ b/") :].strip()
        elif line.startswith("diff --git "):
            parts = line.split(" b/", 1)  # `diff --git a/x b/y` → chemin `b/`
            if len(parts) == 2:
                path = parts[1].strip()
        if not path or path == "/dev/null" or path in seen:
            continue
        seen.add(path)
        yield path


def _detect_review_language(diff: str) -> Optional[str]:
    """Langage dominant d'un diff parmi ceux que ``code_review`` sait reviewer.

    Parse les chemins de fichiers du diff (lignes ``+++ b/…`` / ``diff --git a/… b/…``)
    et compte les extensions reconnues. Retourne le langage le plus représenté, ou
    ``None`` si le diff ne contient **aucun** fichier de code supporté (que du SQL,
    Markdown, config…). Sans ça, l'``ExpertReviewer`` envoyait tout en ``python`` →
    un diff SQL/JS était jugé « Python cassé » (score 0.00) et bloquait à tort. [#409]
    """
    counts: dict[str, int] = {}
    for path in _diff_paths(diff):
        _root, ext = os.path.splitext(path)
        lang = _REVIEW_LANG_BY_EXT.get(ext.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]  # dominant, déterministe


class ExpertReviewer:
    """Adaptateur :class:`Reviewer` vers l'outil expert ``code_review`` (réel).

    Réutilise l'expert existant (non-goal §9 : ne pas le réécrire). L'exécution
    réelle (analyse statique + boucle agentique LLM, rôle ``REVIEWER``) a lieu en
    ``integration`` ; ``code_review`` n'est importé que paresseusement ici pour ne
    pas alourdir l'import de l'exécuteur. Le **mapping** réponse→outcome
    (:func:`outcome_from_review`) est, lui, pur et testé en CI.
    """

    def __init__(self, *, min_quality: float = DEFAULT_MIN_QUALITY, tool=None):
        self._min_quality = min_quality
        self._tool = tool  # injectable pour les tests ; sinon construit à la volée

    async def review(self, diff: str, ctx, *, issue: Optional[IssueSpec] = None) -> ReviewOutcome:
        from collegue.tools.code_review.models import CodeReviewRequest

        language = _detect_review_language(diff)
        if language is None:
            # Aucun fichier de code supporté (python/js/ts/php) dans le diff.
            # #437 : si l'issue attend une IMPLÉMENTATION, un diff sans code est
            # une livraison fantôme probable (cas réel : « export PDF » fermé par
            # +1 ligne de requirements) → BLOQUANT, plus un skip neutre.
            if issue is not None and issue_expects_code(issue):
                return ReviewOutcome(
                    summary=(
                        "revue bloquante : le diff ne contient AUCUN fichier de code supporté "
                        "(python/js/ts/php) alors que l'issue attend une implémentation — "
                        "livraison fantôme probable (#437)"
                    ),
                    quality_score=0.0,
                    findings=(
                        ReviewFindingLite(
                            category="adequacy",
                            severity="critical",
                            title="diff sans code pour une issue d'implémentation",
                        ),
                    ),
                    blocking=True,
                )
            # Livrable non-code ATTENDU (docs, config, données) ou pas d'issue : la
            # revue experte, calibrée pour le code, n'a rien à juger. On NE la lance
            # PAS (sinon du SQL/Markdown serait noté comme du Python cassé → 0.00 →
            # blocage à tort). Outcome neutre, NON bloquant. [#409]
            return ReviewOutcome(
                summary="revue experte ignorée : aucun fichier de code supporté (python/js/ts/php) dans le diff",
                quality_score=1.0,
                findings=(),
                blocking=False,
            )

        tool = self._tool or self._build_tool()
        request = CodeReviewRequest(
            code=diff or "(diff vide)",
            language=language,
            context=issue.to_prompt() if issue is not None else None,
        )
        response = await tool.execute_async(request, ctx=ctx)
        return outcome_from_review(response, min_quality=self._min_quality)

    @staticmethod
    def _build_tool():  # pragma: no cover - chemin réel (integration)
        from collegue.tools.code_review.tool import CodeReviewTool

        return CodeReviewTool()
