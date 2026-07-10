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

import base64
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, replace
from typing import Iterator, List, Optional, Protocol, Tuple, runtime_checkable

from collegue.executor.agent import IssueSpec
from collegue.sandbox.executor import DockerSandbox
from collegue.textnorm import inline

# `python -m pytest` (et non le script `pytest`) ajoute le répertoire de travail
# (la racine du workspace, montée sur `/workspace`) à `sys.path`. Sans ça, les tests
# d'un projet en layout `src/`/`app/` qui importent par package (`from app.x import …`,
# `from src.x import …`) lèvent `ModuleNotFoundError` à la collecte → le gate échoue
# à tort (tests verts vus comme rouges). Voir issue #413.
# #478 : en non-tty (conteneur), pytest borne son short summary à COLUMNS
# (défaut 80) et tronque le diagnostic avec « ... » — le nom du paquet manquant
# (httpx, python-multipart…) disparaissait du feedback de retry, brûlant des
# cycles à deviner. Largeur large forcée sur TOUTES les invocations pytest du
# gate (préfixe d'env sh — inerte pour pip/uvicorn).
_PYTEST_WIDE_COLUMNS = "COLUMNS=220"
DEFAULT_TEST_COMMAND = f"{_PYTEST_WIDE_COLUMNS} python -m pytest -q"
# Note visible dans la sortie du gate quand l'installation des deps échoue (#414).
_INSTALL_FAILED_NOTE = "[gate] installation des dépendances en échec — tests lancés quand même (#414)"


def deps_install_prelude(workspace: str, *, strict: bool = False, use_cache: bool = False) -> Optional[str]:
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
    # #496 : ``use_cache=True`` retire ``--no-cache-dir`` — réservé au cas où le
    # sandbox monte un cache pip persistant (sinon le cache irait dans le tmpfs
    # /tmp, compté dans --memory). Dérivé du sandbox côté run_quality_gate.
    no_cache = "" if use_cache else "--no-cache-dir "
    parts: List[str] = []
    if os.path.isfile(os.path.join(workspace, "requirements.txt")):
        parts.append(f"python -m pip install --user {no_cache}-q -r requirements.txt")
    if os.path.isfile(os.path.join(workspace, "pyproject.toml")) or os.path.isfile(os.path.join(workspace, "setup.py")):
        parts.append(f"python -m pip install --user {no_cache}-q -e .")
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

# Méthodes HTTP reconnues dans la syntaxe enrichie des chemins sondés (#483) :
# « POST:/auth/register ». Sans préfixe reconnu → GET (compat #458).
_SMOKE_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
# Chemins sondés par défaut (#483) : racine + routes d'auth — le flux d'écriture
# central, mort « out-of-the-box » deux runs de suite avec un smoke GET-only.
# Défaut de SIGNATURE (et pas seulement de config) : un appelant qui active
# smoke_run sans fournir de chemins (ex. harness qui bypasse _gate_options)
# bénéficie de la couverture — sinon le fix serait inerte au run réel, comme
# #461 l'a été en v4. Coût nul sur une app sans ces routes (404/405 < 500).
DEFAULT_SMOKE_PATHS = ("/", "POST:/auth/register", "POST:/auth/login")
# #503 : origine cross-origin envoyée par chaque sonde — distincte de l'app
# (127.0.0.1:8765). Un backend sans CORS ne renvoie pas d'Access-Control-Allow-
# Origin couvrant cette origine → l'UI réelle serait bloquée au premier fetch
# (run v5 : impossible de s'inscrire depuis l'interface). Défaut de SIGNATURE
# (actif dès smoke_run, même si le harness bypasse _gate_options). Le port Vite
# usuel des fronts générés. Vide = contrôle CORS désactivé (apps sans front).
_SMOKE_DEFAULT_ORIGIN = "http://localhost:5173"
# Payload générique des sondes à corps (#483) : champs usuels des routes d'auth.
# Pydantic ignore les champs en trop par défaut → une route register/login VALIDE
# ce corps et exécute son handler (le 500 passlib/bcrypt « out-of-the-box » de
# FacNor v4 devient visible) ; un modèle strict répond 422 — toléré, l'app a répondu.
_SMOKE_PROBE_PAYLOAD = json.dumps(
    {
        "email": "smoke-458@example.com",
        "username": "smoke458",
        "password": "Smoke-Run-458!",
        "full_name": "Smoke Run",
        "name": "Smoke Run",
    }
).encode()

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
paths = %(paths)r  # paires (méthode, chemin) — cf. _smoke_probe_script (#483)
payload = %(payload)r
timeout = %(timeout)r
origin = %(origin)r  # #503 : origine cross-origin (vide = contrôle CORS désactivé)
log = open("/tmp/.collegue_smoke.log", "w+", encoding="utf-8", errors="replace")
proc = subprocess.Popen(command, shell=True, stdout=log, stderr=subprocess.STDOUT)
base = "http://127.0.0.1:%(port)d"
deadline = time.time() + timeout
verdicts = []
for method, path in paths:
    status = None
    acao = None  # Access-Control-Allow-Origin renvoyé (#503)
    attempted = False
    # Au moins UNE tentative par chemin, même si le précédent a épuisé le délai
    # (sinon : faux rouge « sans réponse » sur un chemin jamais contacté).
    while not attempted or time.time() < deadline:
        attempted = True
        if proc.poll() is not None:
            break  # le serveur est mort avant de répondre
        try:
            data = payload if method not in ("GET", "HEAD") else None
            headers = {"Origin": origin} if origin else {}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
            resp = urllib.request.urlopen(req, timeout=2)
            status = resp.status
            acao = resp.headers.get("Access-Control-Allow-Origin")
            break
        except urllib.error.HTTPError as exc:
            status = exc.code
            acao = exc.headers.get("Access-Control-Allow-Origin") if exc.headers else None
            break
        except Exception:
            time.sleep(0.5)
    verdicts.append((method, path, status, acao))
time.sleep(0.3)  # resserre la fenêtre « répond une fois puis meurt »


def _cors_ok(status, acao):
    # #503 : CORS contrôlé seulement quand l'app A répondu et a ACCEPTÉ la requête
    # (status < 400). Un 4xx de contrat est déjà signalé par le status ; un 401
    # protégé n'a pas à exposer CORS. « * » ou écho exact de l'origine = OK.
    if not origin or status is None or status >= 400:
        return True
    return acao in ("*", origin)


cors_failures = [(m, p) for m, p, s, a in verdicts if not _cors_ok(s, a)]
ok = (
    proc.poll() is None
    and all(s is not None and s < 500 for _m, _p, s, _a in verdicts)
    and not cors_failures
)
for method, path, status, acao in verdicts:
    print("[gate] smoke run", method, path, "->", status if status is not None else "sans réponse")
for method, path in cors_failures:
    print(
        "[gate] smoke run CORS ABSENT", method, path, "— l'origine", origin,
        "n'est pas autorisée (Access-Control-Allow-Origin) ; l'UI serait bloquée au premier fetch (#503)",
    )
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


def _smoke_probe_script(
    command: str,
    paths: Tuple[str, ...],
    timeout: float,
    *,
    port: int = _SMOKE_PORT,
    origin: str = _SMOKE_DEFAULT_ORIGIN,
) -> str:
    """Source python de la sonde smoke-run (pur — testable par ``compile``)."""
    # #483 : préfixe « MÉTHODE: » optionnel (whitelist _SMOKE_METHODS) — sans
    # lui, GET (compat #458). Les méthodes à corps envoient le payload JSON
    # générique : 4xx toléré (validation — l'app A répondu), 5xx rouge.
    # `/` de tête normalisé : sans lui, urlopen("…:8765health") lèverait à
    # chaque tentative et brûlerait tout le délai en silence.
    probes = []
    for raw in paths:
        method, sep, rest = raw.partition(":")
        if sep and method.strip().upper() in _SMOKE_METHODS:
            method, path = method.strip().upper(), rest.strip()
        else:
            method, path = "GET", raw
        probes.append((method, path if path.startswith("/") else "/" + path))
    normalized = tuple(probes) or (("GET", "/"),)
    return _SMOKE_PROBE_TEMPLATE % {
        "command": command,
        "paths": normalized,
        "payload": _SMOKE_PROBE_PAYLOAD,
        "timeout": float(timeout),
        "port": int(port),
        "origin": str(origin),
    }


def smoke_run_command(
    workspace: str,
    *,
    command: Optional[str] = None,
    paths: Tuple[str, ...] = DEFAULT_SMOKE_PATHS,
    timeout: float = 30.0,
    cors_origin: str = _SMOKE_DEFAULT_ORIGIN,
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
      l'app a répondu ; 5xx ou silence = rouge). Préfixe « MÉTHODE: » optionnel
      (#483, ex. ``POST:/auth/register``) : les méthodes à corps envoient un
      payload JSON générique (champs usuels d'auth) — un register qui crashe à
      l'exécution (500 out-of-the-box) ne passe plus avec un simple GET vert ;
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
    script = _smoke_probe_script(command, paths, timeout, origin=cors_origin)
    return f"python - <<'{_SMOKE_HEREDOC}'\n{script}{_SMOKE_HEREDOC}"


# ── Passe E2E navigateur (#503 suivi) : l'UI RÉELLE contre l'API ────────────────
_E2E_BANNER = "[gate] e2e navigateur : l'UI rend et appelle le backend (#503)"
# On SERT le front sur 5173 (et pas le 4173 par défaut de `vite preview`) : c'est
# l'origine conventionnelle que les backends générés autorisent en CORS (cf.
# _SMOKE_DEFAULT_ORIGIN) — sinon le navigateur serait bloqué par CORS sur un app
# pourtant bien intégrée (faux échec, observé au 1er essai de la sonde réelle).
_E2E_FRONT_PORT = 5173
_E2E_HEREDOC = "COLLEGUE_E2E_503"
# Noms d'env usuels par lesquels un frontend lit la base d'URL de l'API — on les
# injecte TOUS au build (Vite/CRA/Next inlinent au build) pour pointer le front
# vers le backend du gate, sans hypothèse de framework.
_E2E_API_ENV_VARS = (
    "VITE_API_BASE_URL",
    "VITE_API_URL",
    "REACT_APP_API_URL",
    "NEXT_PUBLIC_API_URL",
    "API_BASE_URL",
)

# Sonde E2E exécutée DANS le conteneur du gate (Playwright python + chromium bakés).
# Attend le serveur front, charge l'UI dans chromium headless, et vérifie : la page
# REND (pas de page blanche) ET aucun appel vers le backend n'échoue (un contrat
# divergent / préfixe fantôme / CORS absent / mauvaise base d'URL se manifeste en
# requestfailed vers l'origine backend). %%-formaté (le code garde ses accolades).
_E2E_BROWSER_TEMPLATE = """\
import sys, time, urllib.error, urllib.request

FRONT = %(front_url)r
BACKEND = %(backend_origin)r  # "host:port" — un requestfailed vers là = intégration rompue
TIMEOUT_MS = %(timeout_ms)d

# 1. Attendre que le serveur de preview réponde (goto échouerait sur connexion refusée).
deadline = time.time() + 40
up = False
while time.time() < deadline:
    try:
        urllib.request.urlopen(FRONT, timeout=2)
        up = True
        break
    except urllib.error.HTTPError:
        up = True  # a répondu (même 4xx) = serveur vivant
        break
    except Exception:
        time.sleep(0.5)
if not up:
    print("[gate] e2e ÉCHEC : le serveur frontend n'a pas démarré sur", FRONT)
    sys.exit(1)

from playwright.sync_api import sync_playwright

console_errors = []
failed = []
status = None
body = ""
html = ""
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("requestfailed", lambda r: failed.append(r.url + " :: " + (r.failure or "")))
    try:
        resp = page.goto(FRONT, wait_until="networkidle", timeout=TIMEOUT_MS)
        status = resp.status if resp else None
    except Exception as exc:
        print("[gate] e2e ÉCHEC : la page n'a pas chargé —", str(exc)[:200])
        browser.close()
        sys.exit(1)
    page.wait_for_timeout(2500)  # laisse les fetch d'amorçage (ex. /health) partir
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    html = page.content()
    browser.close()

rendered = len(body.strip()) > 0 or any(
    tag in html.lower() for tag in ("<div", "<main", "<button", "<form", "<header", "<nav", "<section")
)
backend_failures = [u for u in failed if BACKEND in u]
problems = []
if status is None or status >= 400:
    problems.append("la page racine répond " + str(status))
if not rendered:
    problems.append("page blanche : aucun contenu rendu (l'app ne monte pas dans le navigateur)")
if backend_failures:
    problems.append(
        "appel(s) au backend en ÉCHEC depuis l'UI (intégration front<->back rompue : "
        "contrat/préfixe/CORS/base d'URL) : " + " ; ".join(backend_failures[:5])
    )

print("[gate] e2e :", "OK — l'UI rend et joint le backend" if not problems else "ÉCHEC")
for p in problems:
    print("[gate] e2e -", p)
if console_errors:
    print("[gate] e2e (info) erreurs console :", " | ".join(console_errors[:5]))
sys.exit(0 if not problems else 1)
"""


def _e2e_browser_script(front_url: str, backend_origin: str, timeout: float) -> str:
    """Source de la sonde E2E (pure → testable par ``compile``)."""
    return _E2E_BROWSER_TEMPLATE % {
        "front_url": front_url,
        "backend_origin": backend_origin,
        "timeout_ms": int(max(5.0, timeout) * 1000),
    }


def e2e_gate_command(
    workspace: str,
    *,
    backend_command: Optional[str] = None,
    front_subdir: Optional[str] = None,
    timeout: float = 90.0,
) -> Optional[str]:
    """Passe E2E navigateur (#503 suivi) : charge l'UI RÉELLE dans chromium headless
    et vérifie qu'elle REND et que ses appels au backend ABOUTISSENT. Fail-closed.

    Comble le trou de fond de #503 : le smoke sonde le backend en HTTP DIRECT et le
    gate frontend BUILDE le front, mais RIEN n'exerce l'UI CONTRE l'API — un contrat
    d'API divergent, un préfixe ``/api`` fantôme, un CORS absent ou une mauvaise base
    d'URL passent tous les gates. Cette passe démarre le backend + sert le front (avec
    la base d'URL backend injectée au build) + pilote un vrai navigateur.

    Ne s'active que pour un projet **full-stack** : backend ASGI détecté (ou
    ``backend_command``) ET frontend avec scripts ``build`` + ``preview`` (Vite & co).
    ``None`` sinon (skip — pas de faux rouge sur un projet non concerné). S'exécute
    dans le conteneur du gate (Playwright + chromium bakés dans l'image).
    """
    target = backend_command or _detect_asgi_app(workspace)
    if target is None:
        return None
    candidates = [front_subdir] if front_subdir is not None else _frontend_dirs(workspace)
    front = None
    for sub in candidates:
        base = workspace if sub == "." else os.path.join(workspace, sub)
        pkg = os.path.join(base, "package.json")
        if not os.path.isfile(pkg):
            continue
        try:
            with open(pkg, encoding="utf-8") as handle:
                scripts = dict((json.load(handle) or {}).get("scripts") or {})
        except (OSError, ValueError):
            continue
        # besoin de servir le BUILD : build + preview (convention Vite des fronts générés).
        if "build" in scripts and "preview" in scripts:
            front = sub
            break
    if front is None:
        return None

    be_url = f"http://127.0.0.1:{_SMOKE_PORT}"
    backend = backend_command or f"python -m uvicorn {target} --host 127.0.0.1 --port {_SMOKE_PORT}"
    env_inject = " ".join(f"{name}={shlex.quote(be_url)}" for name in _E2E_API_ENV_VARS)
    install = "(npm ci --no-audit --no-fund --silent || npm install --no-audit --no-fund --silent)"
    cd = "" if front == "." else f"cd {shlex.quote(front)} && "
    script = _e2e_browser_script(f"http://127.0.0.1:{_E2E_FRONT_PORT}", f"127.0.0.1:{_SMOKE_PORT}", timeout)
    # La sonde est écrite dans un FICHIER (puis exécutée) plutôt qu'en ``python -
    # <<HD`` : la commande NE se termine donc PAS par un heredoc nu, et peut être
    # chaînée AVANT la passe smoke (qui, elle, DOIT rester le heredoc final).
    return (
        f"({backend} > /tmp/.e2e_backend.log 2>&1 &) "
        f"&& {cd}{install} > /tmp/.e2e_npm.log 2>&1 "
        f"&& {env_inject} npm run build > /tmp/.e2e_build.log 2>&1 "
        f"&& (npm run preview -- --host 127.0.0.1 --port {_E2E_FRONT_PORT} > /tmp/.e2e_front.log 2>&1 &) "
        f"&& cat > /tmp/.collegue_e2e.py <<'{_E2E_HEREDOC}'\n{script}\n{_E2E_HEREDOC}\npython /tmp/.collegue_e2e.py"
    )


def installability_command(workspace: str, *, use_cache: bool = False) -> Optional[str]:
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
    # #496 : --no-cache-dir retiré si un cache pip est monté (use_cache).
    pip_flags = f"{'' if use_cache else '--no-cache-dir '}-q --retries 5 --timeout 30"
    return (
        f"python -m venv --clear {_GATE_VENV}"
        f" && {_GATE_VENV}/bin/python -m pip install {pip_flags} -r requirements.txt"
        f" && {_GATE_VENV}/bin/python -m pip install {pip_flags} pytest"
        f" && {_PYTEST_WIDE_COLUMNS} {_GATE_VENV}/bin/python -m pytest --collect-only -q"
        " --continue-on-collection-errors"
    )


# #481 : module importé → paquet PyPI quand les noms divergent (cas connus du
# run FacNor v4 + classiques). Heuristique sinon : nom-module ≈ nom-paquet.
_MODULE_TO_PACKAGE = {
    "jose": "python-jose[cryptography]",
    "email_validator": "email-validator",
    "multipart": "python-multipart",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "fitz": "PyMuPDF",
}
_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z0-9_.]+)'")
# #501 : formes « paquet auto-diagnostiqué » — Starlette/FastAPI/pydantic
# interceptent l'import manquant et lèvent LEUR message ; le nom capturé est
# DÉJÀ un paquet PyPI (pas un module). 4 alternatives couvrant les formes réelles
# observées (run v5 + tests pipeline #478) — l'ancrage [A-Za-z0-9] exclut tout
# flag pip (« -r », « --no-cache ») :
#   - requires "X" to be installed   (python-multipart, guillemets)
#   - requires the X package         (httpx via starlette.testclient, sans guillemets)
#   - please install X               (générique)
#   - pip install X                  (« You can install it with pip install X »)
_SELFDIAGNOSED_PACKAGE_RE = re.compile(
    r"""(?:requires|needs)\s+["']([A-Za-z0-9][A-Za-z0-9._-]*)["']\s+to\s+be\s+installed"""
    r"""|requires\s+the\s+([A-Za-z0-9][A-Za-z0-9._-]*)\s+package\s+to\s+be\s+installed"""
    r"""|please\s+install\s+([A-Za-z0-9][A-Za-z0-9._-]*)"""
    r"""|pip\s+install\s+([A-Za-z0-9][A-Za-z0-9._-]*)"""
)
# #501 : mots génériques captés à tort par les formes larges (« please install
# the dependency », « pip install git+… ») — un nom bidon échouerait de toute
# façon à l'install (gate rouge, fail-closed), mais on évite la pollution de
# requirements.txt en amont.
_SELFDIAGNOSED_DENYLIST = frozenset({"the", "a", "an", "it", "this", "that", "your", "my", "git", "user", "package"})
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)")
# FacNor v4 tâche 2 : 3 paquets découverts en série (chaîne d'imports — Python ne
# révèle que le PREMIER module manquant d'un fichier, quel que soit le flag pytest).
_MAX_REMEDIATION_ROUNDS = 3


def _canonical(name: str) -> str:
    """Nom de paquet normalisé PEP 503 (comparaisons requirements)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def missing_modules(output: str) -> List[str]:
    """Modules de premier niveau des ``ModuleNotFoundError`` (dédupliqués, ordre stable)."""
    seen: List[str] = []
    for match in _MISSING_MODULE_RE.finditer(output or ""):
        module = match.group(1).split(".")[0]
        if module and module not in seen:
            seen.append(module)
    return seen


def selfdiagnosed_packages(output: str) -> List[str]:
    """Paquets PyPI nommés par les messages auto-diagnostiqués (#501).

    Starlette/FastAPI/pydantic interceptent l'``ImportError`` et lèvent « X
    requires "Y" to be installed » / « requires the Y package » / « please
    install Y » : le nom capturé est DÉJÀ un paquet (pas un module), à ajouter
    tel quel à ``requirements.txt``. Au run v5, « Form data requires
    "python-multipart" » a brûlé une tentative car ce n'est pas une
    ModuleNotFoundError. Dédupliqué (PEP 503), ordre stable.
    """
    seen: List[str] = []
    seen_keys: set = set()
    for match in _SELFDIAGNOSED_PACKAGE_RE.finditer(output or ""):
        name = next((g for g in match.groups() if g), None)
        if not name or name.lower() in _SELFDIAGNOSED_DENYLIST:
            continue
        key = _canonical(name)
        if key not in seen_keys:
            seen_keys.add(key)
            seen.append(name)
    return seen


def requirement_for_module(module: str) -> str:
    """Paquet PyPI proposé pour un module manquant (table, sinon heuristique _ → -)."""
    return _MODULE_TO_PACKAGE.get(module, module.replace("_", "-"))


def _is_local_module(workspace: str, module: str) -> bool:
    """Vrai si ``module`` correspond à un fichier/répertoire du projet (#481).

    Cherche ``mod``/``mod.py`` à la racine, sous ``src/`` ET sous chaque
    sous-répertoire de premier niveau : un import plat intra-package
    (``app/main.py`` qui fait ``import utils`` pour ``app/utils.py``) lève le
    même ``ModuleNotFoundError`` qu'un paquet manquant — l'installer depuis
    PyPI serait une dependency confusion (le gate pourrait même VERDIR sur la
    sémantique d'un paquet homonyme étranger).
    """
    prefixes = ["", "src"]
    try:
        prefixes += [
            entry
            for entry in os.listdir(workspace)
            if not entry.startswith(".") and entry != "node_modules" and os.path.isdir(os.path.join(workspace, entry))
        ]
    except OSError:
        pass
    return any(
        os.path.exists(os.path.join(workspace, prefix, suffix))
        for prefix in prefixes
        for suffix in (module, f"{module}.py")
    )


def remediate_missing_requirements(workspace: str, output: str) -> Tuple[str, ...]:
    """Ajoute à ``requirements.txt`` les paquets des modules manquants (#481).

    Remédiation **déterministe, sans LLM** : « module X introuvable en venv nu »
    se répare en ajoutant le paquet à ``requirements.txt`` — repasser par un
    cycle génération + gate complet coûtait un cycle PAR paquet (77,6 % du
    budget tokens du run v4 brûlé en tentatives échouées, majoritairement sur
    cette classe). Garde-fous, tous INDISPENSABLES :

    - ``requirements.txt`` doit exister (on ne crée pas le contrat d'install) ;
    - module LOCAL du workspace (``mod/``, ``mod.py``, ``src/mod``) jamais
      ajouté — un layout ``src/`` ferait installer un paquet PyPI homonyme du
      projet (dependency confusion) ;
    - module de la stdlib jamais ajouté ;
    - paquet déjà déclaré (nom PEP 503, extras/pins ignorés) jamais dupliqué.

    Retourne les paquets ajoutés (tuple vide si rien à faire).
    """
    req_path = os.path.join(workspace, "requirements.txt")
    if not os.path.isfile(req_path):
        return ()
    modules = missing_modules(output)
    # #501 : les messages auto-diagnostiqués (Starlette « requires "X" ») ne sont
    # PAS des ModuleNotFoundError — sans cette source, l'early-return ci-dessous
    # raterait le cas multipart/httpx du run v5.
    packages = selfdiagnosed_packages(output)
    if not modules and not packages:
        return ()
    try:
        with open(req_path, encoding="utf-8") as handle:
            existing_text = handle.read()
    except (OSError, UnicodeDecodeError):
        # Fichier illisible/non-UTF8 : pas de remédiation (et surtout pas de
        # réécriture qui corromprait le fichier) — cycle LLM normal.
        return ()
    declared = set()
    for line in existing_text.splitlines():
        match = _REQ_NAME_RE.match(line)
        if match:
            declared.add(_canonical(match.group(1)))
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    additions: List[str] = []
    for module in modules:
        if module in stdlib:
            continue
        if _is_local_module(workspace, module):
            continue  # module local du projet, pas un paquet PyPI
        package = requirement_for_module(module)
        if _canonical(_REQ_NAME_RE.match(package).group(1)) in declared:
            continue  # déjà déclaré : manquant pour une AUTRE raison (pin cassé…)
        additions.append(package)
        declared.add(_canonical(_REQ_NAME_RE.match(package).group(1)))
    # #501 : paquets auto-diagnostiqués (déjà des noms PyPI — PAS de
    # requirement_for_module, PAS de filtre stdlib). Mêmes gardes : dédup via
    # `declared` (mis à jour ci-dessus, donc l'ordre modules-puis-paquets évite
    # les doublons) et anti-module-local (« please install utils » pour un
    # utils.py local = dependency confusion).
    for package in packages:
        base = _REQ_NAME_RE.match(package)
        if base is None:
            continue
        key = _canonical(base.group(1))
        if key in declared or _is_local_module(workspace, base.group(1)):
            continue
        additions.append(package)
        declared.add(key)
    if not additions:
        return ()
    body = existing_text if existing_text.endswith("\n") or not existing_text else existing_text + "\n"
    with open(req_path, "w", encoding="utf-8") as handle:
        handle.write(body + "\n".join(additions) + "\n")
    return tuple(additions)


# #463 : note visible quand « aucun test collecté » (pytest exit 5) est toléré.
_EXIT5_NOTE = "[gate] pytest : aucun test collecté (exit 5) — toléré, une passe frontend couvre (#463)"


def _tolerate_pytest_exit5(command: str) -> str:
    """Mappe l'exit 5 de pytest (« aucun test collecté ») vers un succès (#463).

    Sur les tâches greenfield/frontend, ne collecter aucun test pytest est
    NORMAL (le livrable est du TS/JS, couvert par la passe frontend #438) — mais
    le gate fail-closed confondait exit 5 et tests rouges : la tâche brûlait ses
    tentatives sur un non-échec puis figeait le DAG. La connaissance vivait dans
    une rustine du harness de validation au lieu du moteur. N'est appliqué que
    quand une passe frontend va EFFECTIVEMENT tourner (fail-closed sinon) ;
    tout autre exit non nul reste un échec.
    """
    return f"({command}); _rc=$?; if [ $_rc -eq 5 ]; then echo {shlex.quote(_EXIT5_NOTE)}; elif [ $_rc -ne 0 ]; then exit $_rc; fi"


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
    """Verdict d'adéquation diff↔issue (#437) + couverture des critères (#499)."""

    implemented: bool
    justification: str = ""
    # #499 : les critères CHIFFRABLES/observables de l'issue sont-ils ASSERTÉS par
    # au moins un test du diff ? None = non évalué (rétrocompat #437 : un checker
    # qui ne le renseigne pas ne bloque jamais sur ce volet).
    tests_assert_criteria: Optional[bool] = None
    tests_justification: str = ""


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
    # #481 : paquets ajoutés à requirements.txt par la remédiation déterministe
    # du gate (modules manquants) — visibles dans la PR et l'audit.
    requirements_added: Tuple[str, ...] = ()
    # #482 : lignes de requirements.txt de la base supprimées sans que l'issue
    # le demande — requirements.txt est append-only ; non vide ⇒ gate rouge.
    requirements_removed: Tuple[str, ...] = ()
    # #497 : lignes de paquet ajoutées à requirements.txt SANS contrainte de
    # version (dépendances directes non épinglées) — signal NON bloquant ; cause
    # racine du register→500 v4 (passlib figé + bcrypt résolu librement).
    requirements_unpinned: Tuple[str, ...] = ()
    # #499 : les critères chiffrables de l'issue sont-ils assertés par les tests
    # du diff ? None = non évalué (rétrocompat) ; False ⇒ gate rouge (un test qui
    # n'asserte que « 200 » a livré la TVA ×100 du run v5 sans rien voir).
    adequacy_tests_assert: Optional[bool] = None
    adequacy_tests_justification: str = ""
    # #508 : fichiers parasites AJOUTÉS au commit (logs, bases, .env, node_modules,
    # __pycache__) — signal NON bloquant par défaut ; rouge seulement si
    # forbidden_files_block (opt-in). Récidive v4→v5 : `server.log` livré.
    forbidden_files: Tuple[str, ...] = ()
    # Vrai quand forbidden_files a RÉELLEMENT fait rougir le gate (block opt-in actif
    # ET liste non vide) — distinct du simple signal. Permet à failure_feedback de ne
    # surfacer la consigne « retire ces fichiers » que lorsqu'elle est la cause du
    # rejet (sinon, en mode signal, elle masquerait le vrai motif d'échec). Run v6 :
    # `server.log` bloquait le gate mais le feedback montrait du bruit pip trompeur.
    forbidden_files_blocking: bool = False
    # §4.7 (Phase B) : tests d'acceptation EXÉCUTABLES dérivés des critères du SPEC,
    # écrits par un rôle INDÉPENDANT du coder et lancés en sandbox → verdict OBJECTIF
    # (exit code pytest), pas un avis LLM. None = non évalué quand le checker est
    # absent ; dès qu'il est activé, False/None/skip/erreur rendent le gate rouge.
    # La génération plan-time indépendante et hashée reste le lot suivant (#582).
    acceptance_passed: Optional[bool] = None
    acceptance_output: str = ""
    acceptance_error: Optional[str] = None

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
        if self.requirements_added:
            lines.append(
                "> 🔧 **dépendances manquantes ajoutées automatiquement** à `requirements.txt` (#481) : "
                + ", ".join(f"`{p}`" for p in self.requirements_added)
            )
        if self.requirements_removed:
            lines.append(
                "> ⛔ **lignes de `requirements.txt` supprimées sans que l'issue le demande (#482)** — "
                "`requirements.txt` est append-only. Lignes supprimées : "
                + ", ".join(f"`{_fence_safe_line(line)}`" for line in self.requirements_removed)
            )
        if self.requirements_unpinned:
            lines.append(
                "> ⚠️ **dépendances directes non épinglées** ajoutées à `requirements.txt` (#497) — "
                "une version résolue librement peut casser l'installation vierge (cause du register→500 v4). "
                "Épingle-les (`==X.Y.Z` ou bornes `>=A,<B`) : "
                + ", ".join(f"`{_fence_safe_line(line)}`" for line in self.requirements_unpinned)
            )
        if self.forbidden_files:
            lines.append(
                "> 🧹 **fichiers parasites committés (#508)** — artefacts d'exécution / secrets / "
                "bases locales à RETIRER du commit et à ajouter au `.gitignore` : "
                + ", ".join(f"`{_fence_safe_line(p)}`" for p in self.forbidden_files)
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
        if self.adequacy_tests_assert is False:
            lines += [
                "",
                "> ⛔ **couverture des critères par les tests insuffisante (#499)** — un critère "
                "chiffrable/observable de l'issue n'est asserté par aucun test du diff.",
            ]
            if self.adequacy_tests_justification:
                lines.append(f"> {_fence_safe_line(self.adequacy_tests_justification)}")
        if not self.tests_touched:
            lines += [
                "",
                "> ⚠️ **aucun fichier de test touché** par ce diff (#437) — la feature livrée "
                "n'est couverte par aucun test nouveau ou modifié.",
            ]
        if self.acceptance_passed is False:
            lines += [
                "",
                "> ⛔ **tests d'acceptation dérivés du SPEC en ÉCHEC (§4.7)** — des tests exécutables "
                "écrits indépendamment du coder (depuis les critères d'acceptation) ne passent pas sur "
                "le code livré. Vérification objective et non circulaire de la conformité au contrat.",
            ]
        elif self.acceptance_error:
            lines += [
                "",
                f"> ⛔ contrôle d'acceptation (§4.7) indisponible (fail-closed) : "
                f"{_fence_safe_line(self.acceptance_error)}",
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
# --- garde append-only requirements.txt (#482) ---------------------------------
# Nom de paquet en tête d'une ligne de requirements ; les lignes d'option
# (-r/-e/--index-url), commentaires et vides n'ont pas de nom.
_REQ_LINE_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]+\])?")
# #497 : opérateurs de contrainte de version PEP 508. Une ligne qui en porte au
# moins un (après le nom+extras) est « épinglée » ; « @ » (source figée,
# `pkg @ https://…` / `pkg @ git+…`) vaut aussi épinglage.
_REQ_PIN_RE = re.compile(r"(===|==|~=|!=|>=|<=|<|>|@)")


def _requirement_name(line: str) -> Optional[str]:
    """Nom normalisé (PEP 503) d'une ligne de requirements, ``None`` si non-paquet."""
    text = line.strip()
    if not text or text.startswith(("#", "-")):
        return None
    match = _REQ_LINE_NAME_RE.match(text)
    return _canonical(match.group(1)) if match else None


def _requirement_key(line: str) -> Optional[str]:
    """Clé d'identité d'une ligne de requirements pour la garde #482.

    - paquet : nom PEP 503 **+ extras normalisés** — ``passlib[bcrypt]`` ≠
      ``passlib`` : perdre un extra prive le venv nu de la dépendance réelle,
      invisible à la collecte #439 (les backends lazy ne sont importés qu'à
      l'exécution) ;
    - ligne d'option (``-r base.txt``, ``--extra-index-url …``) : la ligne
      normalisée — perdre un ``-r`` jette tout un fichier de dépendances ;
    - commentaire/vide : ``None`` (pas une dépendance).
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("-"):
        return " ".join(text.split())
    match = _REQ_LINE_NAME_RE.match(text)
    if not match:
        return None
    name = _canonical(match.group(1))
    extras = match.group(2)
    if extras:
        items = sorted(item.strip().lower() for item in extras[1:-1].split(","))
        return f"{name}[{','.join(items)}]"
    return name


def requirement_keys_present(workspace: str) -> frozenset:
    """Clés (#482) des paquets présents dans le(s) ``requirements.txt`` du workspace
    APRÈS application du diff.

    Sert à distinguer une SUPPRESSION réelle (le paquet disparaît) d'une simple
    DÉ-DUPLICATION (le paquet RESTE sous une autre ligne — ex. doublon non épinglé
    retiré alors que la version épinglée demeure ; cas réel FacNor v8, tâche 12 :
    requirements.txt avait accumulé ``fastapi==X`` + un doublon nu ``fastapi`` via
    le cycle ré-ajout #482, et le nettoyage du doublon était faussement rejeté).
    Union sur la racine + les sous-répertoires de 1er niveau (layout monorepo #457).
    """
    keys: set = set()
    roots = [workspace]
    try:
        for entry in os.scandir(workspace):
            if entry.is_dir() and not entry.name.startswith("."):
                roots.append(entry.path)
    except OSError:
        pass
    for root in roots:
        try:
            with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as fh:
                # Lecture BORNÉE (#482 suivi v8, revue) : requirements.txt est écrit par
                # l'agent (non fiable, comme dans :func:`_detect_asgi_app`) — un fichier
                # adverse de plusieurs Mo ne doit pas charger le gate. 256 Ko couvre très
                # largement tout requirements.txt légitime.
                blob = fh.read(262144)
            for line in blob.splitlines():
                key = _requirement_key(line)
                if key is not None:
                    keys.add(key)
        except (OSError, UnicodeDecodeError):
            continue
    return frozenset(keys)


def removed_requirement_lines(diff: str, present_keys: frozenset = frozenset()) -> Tuple[str, ...]:
    """Lignes de ``requirements.txt`` de la BASE supprimées par le diff (#482).

    Comparaison par NOM : un changement de pin ou un réordonnancement n'est pas
    une suppression — seule une dépendance qui DISPARAÎT est retenue (cas réel
    FacNor v4, tâche 5 : ``python-jose[cryptography]``/``passlib[bcrypt]``
    perdus à la régénération → « No module named 'jose' » en venv nu, alors que
    12 tests étaient verts).

    ``present_keys`` (#482 suivi v8) : clés des paquets ENCORE présents dans le
    requirements.txt résultant (cf. :func:`requirement_keys_present`). Une ligne
    retirée dont la clé y figure n'est PAS une suppression mais une DÉ-DUPLICATION
    (le paquet reste sous une autre ligne) — retirer un doublon nu ``fastapi`` quand
    ``fastapi==0.137.1`` demeure ne doit pas rougir le gate (cas FacNor v8, tâche
    12, bloquée 3× sur ce faux positif). Vide ⇒ comportement historique (seuls les
    ré-ajouts ``+`` du diff justifient une suppression).
    """
    removed: dict = {}  # nom → ligne d'origine
    added: set = set()
    in_requirements = False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            in_requirements = os.path.basename(path) == "requirements.txt"
            continue
        if line.startswith(("--- ", "+++ ")) or not in_requirements:
            continue
        if line.startswith("-"):
            key = _requirement_key(line[1:])
            if key is not None:
                removed.setdefault(key, line[1:].strip())
        elif line.startswith("+"):
            key = _requirement_key(line[1:])
            if key is not None:
                added.add(key)
    return tuple(text for key, text in removed.items() if key not in added and key not in present_keys)


def unjustified_requirement_removals(
    diff: str, issue: Optional[IssueSpec] = None, present_keys: frozenset = frozenset()
) -> Tuple[str, ...]:
    """Suppressions de requirements NON demandées par l'issue (#482).

    Fail-closed : une suppression est « demandée » si le nom du paquet (ou le
    fichier d'une ligne d'option) apparaît — en MOT ENTIER — dans le texte
    OPÉRATEUR de l'issue (titre, corps, critères). ``issue.context`` est EXCLU :
    machine-généré, il porte le feedback de la tentative précédente, qui NOMME
    précisément les lignes perdues — l'y chercher désarmerait la garde dès la
    tentative 2 (boomerang du feedback). Sans issue, toute suppression bloque.

    Trou connu (documenté) : un RENOMMAGE du fichier (``requirements.txt`` →
    autre nom) évade la garde — mais il évade aussi l'install sandbox et la
    passe #439, donc le gate échoue ailleurs.
    """
    removed = removed_requirement_lines(diff, present_keys=present_keys)
    if not removed or issue is None:
        return removed
    text = " ".join((issue.title or "", issue.body or "", *issue.acceptance_criteria))
    normalized = re.sub(r"[-_.]+", "-", text.lower())
    kept = []
    for line in removed:
        token = _requirement_name(line)
        if token is None:
            # Ligne d'option : le fichier/URL référencé (dernier mot) fait foi.
            parts = line.split()
            token = re.sub(r"[-_.]+", "-", parts[-1].lower()) if len(parts) > 1 else None
        if token and re.search(rf"(?<![a-z0-9-]){re.escape(token)}(?![a-z0-9-])", normalized):
            continue  # suppression demandée par l'issue
        kept.append(line)
    return tuple(kept)


def unpinned_requirement_lines(diff: str) -> Tuple[str, ...]:
    """Lignes de paquet AJOUTÉES à ``requirements.txt`` sans contrainte de version (#497).

    Cause racine du register→500 du run v4 : ``passlib`` 1.7.4 figé + ``bcrypt``
    résolu librement (≥ 5) → incompatible sur installation vierge. Une dépendance
    directe nue (``fastapi``) laisse pip résoudre n'importe quelle version future,
    cassant la reproductibilité. Signal **NON bloquant** : on liste les lignes
    pour la PR (le smoke POST #483 reste le filet aval qui attrape le 500).

    Analyse les lignes ``+`` de ``requirements.txt`` (monorepo-aware, basename) :
    paquet nu → retenu ; paquet contraint (``==``/``>=,<``/``~=``/``pkg @ url``)
    → silencieux ; lignes d'option (``-r``, ``--index-url``), commentaires, vides
    → ignorés. N'analyse que l'argument ``diff`` (= diff de l'agent) : les ajouts
    nus de la remédiation déterministe (#481, ``httpx``) sont écrits APRÈS et
    re-capturés ailleurs — jamais dans ce diff.
    """
    unpinned: List[str] = []
    seen: set = set()
    in_requirements = False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else ""
            in_requirements = os.path.basename(path) == "requirements.txt"
            continue
        if line.startswith(("--- ", "+++ ")) or not in_requirements or not line.startswith("+"):
            continue
        text = line[1:].strip()
        name = _requirement_name(text)  # None pour option/commentaire/vide
        if name is None or name in seen:
            continue
        # Retirer le nom+extras en tête : ce qui reste porte les contraintes.
        # `passlib[bcrypt]==1.7.4` épinglé ; `passlib[bcrypt]` non (l'extra n'est
        # pas une contrainte de version). Le marqueur d'environnement (après
        # « ; », ex. `; python_version<'3.8'`) est retiré : son `<`/`==` n'est
        # pas une contrainte de version du paquet.
        match = _REQ_LINE_NAME_RE.match(text)
        remainder = (text[match.end() :] if match else text).split(";", 1)[0]
        if _REQ_PIN_RE.search(remainder):
            continue
        seen.add(name)
        unpinned.append(text)
    return tuple(unpinned)


_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$|[^/]+[._]test\.[a-z]+$|[^/]+\.spec\.[a-z]+$")


def tests_touched(diff: str) -> bool:
    """Le diff touche-t-il au moins un fichier de test ? (#437, signal de couverture)."""
    return any(_TEST_PATH_RE.search(path.lower()) for path in _diff_paths(diff))


# Motifs de fichiers qui n'ont rien à faire dans un livrable (#508). Récidive
# v4→v5 : `server.log` committé dans le produit. Suffixes de basename (artefacts,
# bases locales, clés/certs privés), variantes `.env` de secrets, et segments de
# chemin (répertoires vendorés/caches).
_FORBIDDEN_FILE_SUFFIXES = (".log", ".db", ".sqlite", ".sqlite3", ".pyc", ".pem", ".key")
_FORBIDDEN_PATH_SEGMENTS = ("node_modules", "__pycache__")
# Variantes `.env` versionnées SCIEMMENT (gabarits sans secret) — à NE PAS flaguer.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _is_env_secret(base: str) -> bool:
    """Vrai pour ``.env`` ET ses variantes de secrets (``.env.local``,
    ``.env.production``…), faux pour les gabarits non sensibles (``.env.example``)."""
    if base == ".env":
        return True
    return base.startswith(".env.") and not base.endswith(_ENV_TEMPLATE_SUFFIXES)


def _is_forbidden_path(path: str) -> bool:
    """Vrai si ``path`` matche un motif de fichier parasite (#508)."""
    base = os.path.basename(path)
    if _is_env_secret(base):
        return True
    if base.endswith(_FORBIDDEN_FILE_SUFFIXES):
        return True
    return any(segment in path.split("/") for segment in _FORBIDDEN_PATH_SEGMENTS)


def forbidden_committed_files(diff: str) -> Tuple[str, ...]:
    """Fichiers AJOUTÉS par le diff dont le chemin matche un motif interdit (#508).

    Artefacts d'exécution (``*.log``, ``*.pyc``, ``__pycache__/``), bases locales
    (``*.db``/``*.sqlite``), dépendances vendorées (``node_modules/``) ou secrets
    (``.env``) n'ont rien à faire dans le livrable. Analyse PURE du diff, aucun
    coût d'infra (comme :func:`unjustified_requirement_removals`, #482) ; ne
    retient que les fichiers NEUFS (marqueur ``new file mode``) — éditer un
    fichier déjà suivi n'est pas le symptôme et resterait silencieux. Résultat
    déterministe (ordre du diff, dédupliqué).
    """
    added: List[str] = []
    seen: set = set()
    current: Optional[str] = None
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1].strip() if len(parts) == 2 else None
        elif line.startswith("new file mode") and current:
            if _is_forbidden_path(current) and current not in seen:
                seen.add(current)
                added.append(current)
    return tuple(added)


_ADEQUACY_SYSTEM = (
    "Tu es un relecteur d'ADÉQUATION (rôle REVIEWER). On te donne une issue (titre, "
    "critères d'acceptation) et le diff livré pour la fermer. Tu ne juges PAS le style : "
    "uniquement si le diff RÉALISE concrètement ce que l'issue demande. "
    "EXCEPTION RÉSULTAT-RUNTIME (#437 suivi v8) : un critère qui exige un RÉSULTAT "
    "D'EXÉCUTION non vérifiable par lecture d'un diff STATIQUE (ex. « la suite de tests "
    "retourne 0 échec sur l'ensemble du code », « le pipeline CI passe », « l'application "
    "démarre et répond ») est DÉJÀ exécuté et vérifié par le gate AVANT toi (tests + smoke + "
    "e2e RÉELS, déjà verts à ce stade) — NE le juge donc PAS sur une preuve statique "
    "impossible : réponds implemented=true dès que le diff MET EN PLACE de façon RÉELLE et "
    "substantielle les ARTEFACTS de la feature (config CI, runner + suite de tests, "
    "intégration), et NOMME ce critère comme vérifié à l'exécution (non statiquement) dans la "
    "justification. "
    'Réponds STRICTEMENT en JSON : {"implemented": true|false, "justification": "..."}. '
    "implemented=false si la feature est ABSENTE ou hors-spec — verrou anti-livraison-fantôme "
    "(ex. : une seule ligne de dépendance pour un service entier, un schéma sans la logique "
    "demandée, des artefacts vides/triviaux)."
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


# #499 : contrôle de COUVERTURE DES TESTS — distinct de l'adéquation #437 (la
# feature est-elle là ?). Ici : les critères chiffrables de l'issue sont-ils
# ASSERTÉS par les tests ? Au run v5, la TVA ×100 a été livrée 12/12 car aucun
# test n'assertait les totaux (status 200 suffisait).
_TEST_ADEQUACY_SYSTEM = (
    "Tu es un relecteur de COUVERTURE DE TEST (rôle REVIEWER). On te donne une issue "
    "(critères d'acceptation) et le diff livré. Tu juges si les critères de VALEUR/CALCUL/"
    "COMPORTEMENT de l'issue sont ASSERTÉS par au moins un test du diff (assertion sur la "
    "VALEUR/le CALCUL/le COMPORTEMENT attendu — montant, total, compteur, numéro séquentiel, "
    "unicité/déduplication EFFECTIVE, rejet d'une entrée invalide —, pas seulement un code HTTP 200). "
    "EXCEPTION RUNTIME (#499) : un critère qui exige une MESURE RUNTIME non visible dans un diff "
    "STATIQUE (ex. « couverture de tests > X% », « latence < Y ms », « débit/perf ») ne peut PAS "
    "être vérifié par lecture du diff — NE bloque PAS dessus : réponds true s'il ajoute des tests "
    "RÉELS et substantiels couvrant le domaine du critère (assertions véritables, jamais des tests "
    "vides/triviaux), et NOMME ce critère comme non vérifiable statiquement dans la justification. "
    "EXCEPTION STRUCTURELLE (#499 suivi v8) : un critère qui exige seulement qu'un élément soit "
    "DÉFINI/PRÉSENT (ex. « le schéma DÉFINIT les tables X/Y avec leurs contraintes d'intégrité », "
    "« le modèle/endpoint EXISTE ») est satisfait par sa PRÉSENCE dans le diff (visible statiquement) "
    "DÈS LORS que des tests RÉELS couvrent le comportement central du critère (au moins une "
    "contrainte/relation REPRÉSENTATIVE assertée). N'EXIGE PAS un test de rejet DÉDIÉ pour CHAQUE "
    "contrainte/colonne sœur : si une contrainte représentative est testée et les autres sont "
    "définies dans le diff, réponds true (« défini » n'est pas « testé »). "
    "Continue de bloquer (false) UNIQUEMENT sur un critère de VALEUR/CALCUL/COMPORTEMENT vérifiable "
    "dans le diff qu'AUCUN test n'asserte (ex. : aucun test n'asserte le montant TTC = somme des "
    "lignes). Si false, COMMENCE la justification par le critère non couvert (concis, actionnable) "
    "AVANT tout commentaire sur ce qui est déjà testé. "
    'Réponds STRICTEMENT en JSON : {"tests_assert_criteria": true|false, "justification": "..."}.'
)


def _parse_test_adequacy(text: str) -> Tuple[bool, str]:
    """Parsing tolérant du verdict de couverture des tests (#499) — fail-closed."""
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return bool(data.get("tests_assert_criteria")), str(data.get("justification") or "")[:500]
        except ValueError:
            pass
    if not raw:
        return False, "réponse vide du contrôle de couverture des tests"
    return False, f"réponse illisible du contrôle de couverture des tests : {raw[:300]}"


# #526 : fichiers GÉNÉRÉS (verrous de dépendances) — volumineux, ~aucune info
# d'adéquation, mais ils SATURENT la fenêtre du juge. Au run v6, un
# ``package-lock.json`` de ~30k chars poussait ``package.json``/``vite.config``/``src/``
# HORS de la fenêtre de 20k → le juge concluait « feature absente » sur un frontend
# pourtant complet (faux rejets en cascade → run bloqué 4/15). On les retire du
# diff soumis au juge (le diff autoritatif, lui, reste intact).
_GENERATED_DIFF_FILES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
)


def strip_generated_from_diff(diff: str) -> str:
    """Retire les blocs ``diff --git`` des fichiers GÉNÉRÉS (lock files) — #526.

    Découpe le diff sur les frontières ``diff --git`` ; un bloc est retiré si le
    BASENAME de son chemin de DESTINATION (``b/…``) est un fichier généré.
    Comparaison par basename EXACT (pas sous-chaîne : ``src/go.sum.parser.ts`` ≠
    ``go.sum``, conservé) et sur la cible ``b/`` (un rename d'un lock vers un
    fichier source est conservé). Robuste : un diff sans ce format (ou vide) est
    renvoyé tel quel.
    """
    if not diff:
        return diff
    blocks = re.split(r"(?=^diff --git )", diff, flags=re.M)
    kept = []
    for block in blocks:
        header = block.split("\n", 1)[0]
        is_generated = False
        if header.startswith("diff --git ") and " b/" in header:
            dest = header.split(" b/", 1)[1].strip()
            is_generated = dest.rsplit("/", 1)[-1] in _GENERATED_DIFF_FILES
        if not is_generated:
            kept.append(block)
    return "".join(kept)


class LLMAdequacyChecker:
    """:class:`AdequacyChecker` par LLM (#437) — fail-closed.

    ``sample_fn`` : ``async (prompt, system_prompt) -> str``, injectable (mocké en
    CI) ; défaut = ``generate_text`` des providers LLM avec la config du serveur.
    Le diff est borné (``max_diff_chars``) pour rester dans la fenêtre du modèle,
    après retrait des fichiers générés (#526) ; une troncature résiduelle est
    ANNONCÉE au juge (sinon il conclut à tort à l'absence d'un fichier non vu).
    """

    def __init__(self, sample_fn=None, *, max_diff_chars: int = 60000):
        self._sample_fn = sample_fn or _default_adequacy_sample_fn()
        self._max_diff_chars = max_diff_chars

    def _diff_for_judge(self, diff: str) -> str:
        """Diff prêt pour le juge : fichiers générés retirés (#526), borné, et
        troncature résiduelle EXPLICITEMENT signalée."""
        cleaned = strip_generated_from_diff(diff or "")
        if not cleaned:
            # Distinguer « diff réellement vide » de « tout le diff était des
            # fichiers générés » : sinon le juge hallucine « livrable absent » sur
            # une tâche dont le livrable serait justement un fichier généré.
            return "(diff = uniquement des fichiers générés/verrous)" if (diff or "").strip() else "(diff vide)"
        if len(cleaned) <= self._max_diff_chars:
            return cleaned
        return (
            cleaned[: self._max_diff_chars]
            + f"\n\n[... DIFF TRONQUÉ à {self._max_diff_chars} chars sur {len(cleaned)} — "
            "NE PAS conclure à l'absence d'un fichier qui n'apparaît pas ci-dessus, "
            "il peut être dans la partie tronquée ...]"
        )

    async def check(self, diff: str, issue: IssueSpec, ctx) -> AdequacyOutcome:
        bounded = self._diff_for_judge(diff)
        prompt = (
            f"## Issue à fermer\n{issue.to_prompt()}\n\n"
            f"## Diff livré\n```diff\n{bounded}\n```\n\n"
            "Ce diff implémente-t-il concrètement l'issue ?"
        )
        text = await self._sample_fn(prompt, _ADEQUACY_SYSTEM)
        outcome = _parse_adequacy(text)
        # #499 : ne contrôler la COUVERTURE des critères que si l'adéquation est
        # OK (sinon déjà rouge), que le diff touche des tests (sinon
        # tests_touched/require_test_changes gère) ET que l'issue porte des
        # critères chiffrables — borne le coût LLM et les faux rouges.
        if not outcome.implemented or not tests_touched(diff) or not issue.acceptance_criteria:
            return outcome
        test_prompt = (
            f"## Issue à fermer\n{issue.to_prompt()}\n\n"
            f"## Diff livré\n```diff\n{bounded}\n```\n\n"
            "Les critères chiffrables/observables de l'issue sont-ils assertés par au moins un test du diff ?"
        )
        test_text = await self._sample_fn(test_prompt, _TEST_ADEQUACY_SYSTEM)
        asserts, justif = _parse_test_adequacy(test_text)
        return replace(outcome, tests_assert_criteria=asserts, tests_justification=justif)


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


# ── §4.7 (Phase B) : tests d'acceptation exécutables, auteur indépendant ─────────


@dataclass(frozen=True)
class AcceptanceOutcome:
    """Verdict des tests d'acceptation §4.7 — exécution OBJECTIVE (exit code pytest)."""

    passed: Optional[bool] = None  # None = non évalué (pas de critères / checker absent)
    output: str = ""
    error: Optional[str] = None  # toute erreur bloque quand le checker est activé
    skipped: bool = False


@runtime_checkable
class AcceptanceChecker(Protocol):
    """Génère des tests d'acceptation depuis les critères du SPEC puis les LANCE (§4.7)."""

    async def check(self, workspace: str, diff: str, issue: IssueSpec, ctx, *, sandbox) -> AcceptanceOutcome: ...


_ACCEPTANCE_SYSTEM = (
    "Tu es un ingénieur QA INDÉPENDANT (tu n'as PAS écrit le code). À partir des critères "
    "d'acceptation d'une issue et du code livré (diff), écris des tests pytest EXÉCUTABLES qui "
    "VÉRIFIENT chaque critère contre le code réel (imports, fixtures, démarrage d'app si nécessaire). "
    "Les tests doivent ÉCHOUER si un critère n'est pas respecté. Réponds UNIQUEMENT avec un module "
    "Python exécutable (aucune prose), en commençant par les imports. Le module doit être "
    "AUTONOME : n'utilise aucun conftest, plugin pytest ni configuration pytest du projet."
)


def _strip_code_fences(text: str) -> str:
    """Retire un éventuel fence Markdown (```python … ```) autour du code généré."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    stripped = stripped.strip()
    return (stripped + "\n") if stripped else ""


def _acceptance_pytest_command(code: str) -> str:
    """Commande pytest isolée pour un module QA non fiable.

    Le module est transporté en base64 puis créé **dans le tmpfs du conteneur**
    avec :func:`tempfile.mkstemp` (nom imprévisible, création exclusive). Il ne
    touche donc jamais un chemin du dépôt choisi par le codeur et il est supprimé
    dans un ``finally``. ``pytest`` est importé sous ``python -I`` *avant* que le
    workspace soit ajouté à ``sys.path`` : un ``pytest.py`` livré par le diff ne
    peut pas détourner le runner. Les conftest, plugins tiers et configurations
    du projet sont également neutralisés ; le code du projet reste importable via
    ``/workspace`` et ``/workspace/src``.
    """
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    launcher = f"""\
import base64
import os
import site
import sys
import tempfile

import pytest

user_site = site.getusersitepackages()
project_paths = [path for path in ("/workspace", "/workspace/src", user_site) if path]
# pytest est déjà importé depuis l'image sous -I : on peut maintenant préfixer
# les dépendances installées --user et le projet sans permettre un pytest.py local.
sys.path[:0] = [path for path in project_paths if path not in sys.path]
os.chdir("/workspace")
fd, path = tempfile.mkstemp(prefix="collegue_acceptance_", suffix=".py", dir="/tmp")
exit_code = 1
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(base64.b64decode({payload!r}))
    exit_code = pytest.main([
        "--noconftest",
        "-c", "/dev/null",
        "--rootdir=/tmp",
        "-q",
        path,
    ])
finally:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
raise SystemExit(exit_code)
"""
    return (
        f"{_PYTEST_WIDE_COLUMNS} PYTHONPATH= PYTEST_ADDOPTS= "
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_PLUGINS= "
        f"python -I -c {shlex.quote(launcher)}"
    )


class LLMAcceptanceChecker:
    """:class:`AcceptanceChecker` — un LLM (rôle REVIEWER, ≠ coder) écrit les tests, la SANDBOX les lance.

    Anti-circularité (§4.7) : le verdict est l'**exit code** de pytest (objectif), pas
    un avis du modèle, et l'auteur des tests est **distinct du coder**. ``sample_fn``
    injectable (mocké en CI). Sans injection, l'appel passe par le ``ctx`` du
    pilote, son timeout/budget et le modèle du rôle REVIEWER. Toute génération
    indisponible ou inexploitable est fail-closed lorsque ce checker est activé.
    Le module généré vit uniquement dans un fichier aléatoire du tmpfs du sandbox,
    puis est supprimé ; il n'entre jamais dans le diff ni dans la PR.
    """

    def __init__(self, sample_fn=None, *, settings_obj=None, max_diff_chars: int = 60000):
        self._sample_fn = sample_fn
        self._settings_obj = settings_obj
        self._max_diff_chars = max_diff_chars

    async def _sample(self, prompt: str, ctx) -> str:
        if self._sample_fn is not None:
            # Compatibilité de l'interface historique utilisée par les tests et
            # les harness : async (prompt, system_prompt) -> str.
            return await self._sample_fn(prompt, _ACCEPTANCE_SYSTEM)
        if ctx is None:
            raise RuntimeError("ctx de sampling absent pour les tests d'acceptation")

        from collegue.config import settings as global_settings
        from collegue.core.llm import LLMRole, model_preferences_for_role
        from collegue.core.llm.client import sample_with_timeout

        settings_obj = self._settings_obj or global_settings
        sample_kwargs = {
            "messages": prompt,
            "system_prompt": _ACCEPTANCE_SYSTEM,
            "temperature": 0.2,
            "max_tokens": int(getattr(settings_obj, "MAX_TOKENS", 8192)),
        }
        preferences = model_preferences_for_role(LLMRole.REVIEWER, settings_obj)
        if preferences:
            sample_kwargs["model_preferences"] = preferences
        result = await sample_with_timeout(ctx, settings_obj=settings_obj, **sample_kwargs)
        return str(getattr(result, "text", "") or "")

    async def check(self, workspace: str, diff: str, issue: IssueSpec, ctx, *, sandbox) -> AcceptanceOutcome:
        if issue is None or not issue.acceptance_criteria:
            return AcceptanceOutcome(skipped=True)
        bounded = strip_generated_from_diff(diff or "")[: self._max_diff_chars]
        prompt = (
            f"## Issue\n{issue.to_prompt()}\n\n"
            f"## Code livré (diff)\n```diff\n{bounded or '(diff vide)'}\n```\n\n"
            "Écris le module de tests pytest d'acceptation."
        )
        try:
            code = _strip_code_fences(await self._sample(prompt, ctx))
        except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException budget remonte
            return AcceptanceOutcome(error=str(exc) or repr(exc))
        if not code.strip():
            return AcceptanceOutcome(error="génération de tests d'acceptation vide")
        prelude = deps_install_prelude(workspace)
        pytest_cmd = _acceptance_pytest_command(code)
        command = f"({prelude}) && {pytest_cmd}" if prelude else pytest_cmd
        res = sandbox.run_tests(workspace, command)
        output = "\n".join(part for part in (res.stdout, res.stderr) if part).strip()
        # pytest exit 5 = AUCUN test collecté → génération inexploitable (0 fonction
        # test_*). Checker activé = contrat exigé : l'absence de verdict bloque.
        if getattr(res, "exit_code", None) == 5:
            return AcceptanceOutcome(
                passed=False,
                error="aucun test d'acceptation collecté (génération inexploitable)",
                output=output,
            )
        return AcceptanceOutcome(passed=bool(res.ok), output=output)


class FakeAdequacyChecker:
    """:class:`AdequacyChecker` déterministe pour la CI (aucun LLM)."""

    def __init__(
        self,
        *,
        implemented: bool = True,
        justification: str = "conforme",
        raises: Optional[Exception] = None,
        tests_assert_criteria: Optional[bool] = None,
        tests_justification: str = "",
    ):
        self._implemented = implemented
        self._justification = justification
        self._raises = raises
        self._tests_assert_criteria = tests_assert_criteria
        self._tests_justification = tests_justification
        self.calls: List[int] = []

    async def check(self, diff: str, issue: IssueSpec, ctx) -> AdequacyOutcome:
        self.calls.append(issue.number)
        if self._raises is not None:
            raise self._raises
        return AdequacyOutcome(
            implemented=self._implemented,
            justification=self._justification,
            tests_assert_criteria=self._tests_assert_criteria,
            tests_justification=self._tests_justification,
        )


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
    acceptance_checker: Optional[AcceptanceChecker] = None,
    require_test_changes: bool = False,
    smoke_run: bool = False,
    smoke_command: Optional[str] = None,
    smoke_paths: Tuple[str, ...] = DEFAULT_SMOKE_PATHS,
    smoke_timeout: float = 30.0,
    smoke_cors_origin: str = _SMOKE_DEFAULT_ORIGIN,
    e2e_gate: bool = True,
    e2e_timeout: float = 90.0,
    fix_missing_requirements: bool = True,
    requirements_guard: bool = True,
    pin_guard: bool = True,
    forbidden_files_guard: bool = True,
    forbidden_files_block: bool = False,
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
    < 500), préfixe « MÉTHODE: » optionnel (#483, ex. ``POST:/auth/register`` —
    payload JSON générique, 4xx toléré, 5xx rouge). Skippée si aucune app n'est
    détectable et qu'aucune commande n'est fournie.
    ``e2e_gate`` (#503 suivi, défaut VRAI) : passe E2E navigateur — démarre le
    backend, build/sert le frontend (base d'URL backend injectée) et pilote
    chromium (Playwright) sur l'UI RÉELLE pour attraper les ruptures front<->back
    (contrat/préfixe/CORS/base d'URL) qu'aucune autre passe n'exerce. Insérée
    AVANT le smoke (sonde écrite en fichier, chaînable). N'a d'effet que sur un
    projet FULL-STACK (backend ASGI + frontend build+preview) — sinon
    :func:`e2e_gate_command` renvoie ``None`` et la passe est inerte. EXIGE une
    image sandbox avec Playwright + chromium (cf. ``docker/sandbox/Dockerfile``) ;
    passer ``e2e_gate=False`` la désactive explicitement.
    """
    sandbox = sandbox or DockerSandbox()
    reviewer = reviewer or _default_reviewer()
    # #496 : le cache pip n'est utilisé que si le sandbox monte effectivement le
    # volume — sinon le cache irait dans le tmpfs /tmp, compté dans --memory.
    # Source de vérité UNIQUE = l'attribut du sandbox (pas un kwarg
    # désynchronisable) ; getattr → tout double de test sans l'attribut → False.
    use_pip_cache = bool(getattr(sandbox, "pip_cache_dir", None))

    # #482 : garde append-only sur requirements.txt — analyse PURE du diff,
    # AVANT les passes d'infra. Les tests tournent quand même : la mémoire de
    # retry (#436) garde son score, et le feedback nominatif part avec.
    requirements_removed: Tuple[str, ...] = ()
    if requirements_guard:
        # #482 suivi v8 : on passe les paquets ENCORE présents dans le requirements.txt
        # résultant — une ligne retirée dont le paquet demeure (doublon nettoyé) n'est
        # pas une suppression bloquante (faux positif qui a bloqué la tâche 12 du run v8).
        present_keys = requirement_keys_present(workspace)
        requirements_removed = unjustified_requirement_removals(diff, issue, present_keys=present_keys)
    # #497 : signal NON bloquant des dépendances directes non épinglées — analyse
    # PURE du diff de l'agent (les ajouts #481 arrivent après, re-capturés
    # ailleurs). N'entre PAS dans would_pass (signal seulement, par défaut).
    requirements_unpinned: Tuple[str, ...] = ()
    if pin_guard:
        requirements_unpinned = unpinned_requirement_lines(diff)
    # #508 : garde fichiers parasites — analyse PURE du diff (comme #482). Signal
    # par défaut ; bloquant seulement si forbidden_files_block (appliqué au verdict).
    forbidden_files: Tuple[str, ...] = ()
    if forbidden_files_guard:
        forbidden_files = forbidden_committed_files(diff)

    # 1. Tests dans le sandbox. Une incapacité à les exécuter = non passé
    #    (fail-closed), pas une exception qui remonterait.
    deps_install_failed = False
    requirements_added: List[str] = []
    try:
        command = test_command
        if install_deps:
            prelude = deps_install_prelude(workspace, strict=require_deps_install, use_cache=use_pip_cache)
            if prelude is not None:
                # Strict (#439) : install bloquante. Toléré (#414) : les tests
                # tournent quand même, l'échec laisse sa note dans la sortie.
                command = f"({prelude}) && {test_command}" if require_deps_install else f"{prelude}; {test_command}"
        front_commands: List[str] = []
        if frontend_gate:
            for front_dir in _frontend_dirs(workspace):
                front = frontend_gate_command(workspace, subdir=front_dir)
                if front is None:
                    continue
                # Une passe PAR répertoire détecté (#457 : la racine ET les
                # sous-répertoires de 1er niveau — layout monorepo).
                banner = _FRONTEND_BANNER if front_dir == "." else f"{_FRONTEND_BANNER} [{front_dir}]"
                front_commands.append(f"echo {shlex.quote(banner)} && ({front})")
        if front_commands:
            # #463 : « aucun test pytest collecté » est NORMAL quand une passe
            # frontend couvre la tâche — exit 5 toléré DANS ce cas seulement.
            command = _tolerate_pytest_exit5(command)
        for front in front_commands:
            # `&&` : chaque passe frontend ne tourne que si la précédente est
            # verte (le verdict est déjà rouge sinon) et son échec rend le
            # gate rouge.
            command = f"({command}) && {front}"
        if check_installability:
            installability = installability_command(workspace, use_cache=use_pip_cache)
            if installability is not None:
                if front_commands:
                    # La collecte de la passe d'installabilité (#439) renvoie
                    # AUSSI exit 5 sans test pytest — même tolérance (#463).
                    installability = _tolerate_pytest_exit5(installability)
                command = f"({command}) && echo '{_INSTALLABILITY_BANNER}' && ({installability})"
        if e2e_gate:
            # #503 (suivi) : passe E2E navigateur — l'UI RÉELLE contre l'API. Insérée
            # AVANT le smoke car sa sonde est écrite dans un fichier (pas un heredoc
            # nu) → chaînable ; le smoke reste le heredoc FINAL. Full-stack uniquement
            # (None sinon). Échec front<->back (contrat/préfixe/CORS/base d'URL) = rouge.
            e2e = e2e_gate_command(workspace, timeout=e2e_timeout)
            if e2e is not None:
                command = f"({command}) && echo {shlex.quote(_E2E_BANNER)} && ({e2e})"
        if smoke_run:
            # #503 (suivi v6) : le contrôle CORS du smoke n'a de sens QUE si un
            # frontend existe — un backend ISOLÉ n'a légitimement pas de middleware
            # CORS et était faussement rejeté (run v6, tâche d'init backend). On
            # n'exige donc le CORS par DÉFAUT que si un frontend est détecté dans le
            # workspace ; un ``smoke_cors_origin`` EXPLICITE (≠ défaut) reste toujours
            # respecté (override opérateur), et "" garde le contrôle désactivé.
            cors = smoke_cors_origin
            if cors == _SMOKE_DEFAULT_ORIGIN and not _frontend_dirs(workspace):
                cors = ""
            smoke = smoke_run_command(
                workspace,
                command=smoke_command,
                paths=smoke_paths,
                timeout=smoke_timeout,
                cors_origin=cors,
            )
            if smoke is not None:
                # Dernière passe (le heredoc doit clore la commande) : l'app est
                # lancée dans le même conteneur, après install des deps (#414).
                command = f"({command}) && echo {shlex.quote(_SMOKE_BANNER)} && {smoke}"
        test_res = sandbox.run_tests(workspace, command)
        if fix_missing_requirements:
            # #481 : une ModuleNotFoundError en venv nu est un trou de
            # requirements.txt, pas un problème de code — remédiation
            # déterministe (table module→paquet) + relance de la MÊME commande,
            # au lieu d'un cycle LLM complet PAR paquet. Borné : une chaîne
            # d'imports ne révèle qu'un module manquant par passage.
            for _ in range(_MAX_REMEDIATION_ROUNDS):
                if test_res.ok:
                    break
                added = remediate_missing_requirements(
                    workspace, "\n".join(part for part in (test_res.stdout, test_res.stderr) if part)
                )
                if not added:
                    break
                requirements_added.extend(added)
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
    would_pass = bool(tests_passed and not review_blocking and review_error is None and not requirements_removed)
    adequacy_implemented: Optional[bool] = None
    adequacy_justification = ""
    adequacy_error: Optional[str] = None
    adequacy_tests_assert: Optional[bool] = None
    adequacy_tests_justification = ""
    if adequacy_checker is not None and issue is not None and would_pass:
        try:
            adequacy = await adequacy_checker.check(diff, issue, ctx)
            adequacy_implemented = bool(adequacy.implemented)
            adequacy_justification = adequacy.justification
            # #499 : couverture des critères par les tests (None = non évalué).
            adequacy_tests_assert = adequacy.tests_assert_criteria
            adequacy_tests_justification = adequacy.tests_justification
        except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
            adequacy_error = str(exc) or repr(exc)

    # §4.7 (Phase B) : tests d'acceptation EXÉCUTABLES (auteur indépendant du coder)
    # lancés en sandbox — verdict OBJECTIF. Seulement si le reste est vert (borne
    # le coût). Dès qu'un checker est fourni, son contrat devient OBLIGATOIRE :
    # issue/critères absents, skip, verdict None ou erreur bloquent fail-closed.
    acceptance_passed: Optional[bool] = None
    acceptance_output = ""
    acceptance_error: Optional[str] = None
    acceptance_required = acceptance_checker is not None
    if acceptance_required and would_pass:
        if issue is None:
            acceptance_error = "issue absente : critères d'acceptation impossibles à vérifier"
        elif not issue.acceptance_criteria:
            acceptance_error = "aucun critère d'acceptation à vérifier"
        else:
            try:
                acc = await acceptance_checker.check(workspace, diff, issue, ctx, sandbox=sandbox)
                acceptance_passed = acc.passed
                acceptance_output = acc.output
                acceptance_error = acc.error
                if acc.skipped:
                    acceptance_passed = None
                    acceptance_error = acceptance_error or "contrôle d'acceptation ignoré sans verdict"
                elif acceptance_passed is None:
                    acceptance_error = acceptance_error or "contrôle d'acceptation terminé sans verdict"
            except Exception as exc:  # noqa: BLE001 - fail-closed ; BaseException (budget) remonte
                acceptance_error = str(exc) or repr(exc)

    touched = tests_touched(diff)
    # #499 : `is not False` — None (non évalué) et True passent ; seul False
    # bloque (rétrocompat #437 : un checker qui n'évalue pas la couverture ne
    # rend jamais le gate rouge sur ce volet). §4.7 est opt-in : checker absent =
    # comportement historique ; checker présent = seul True sans erreur passe.
    acceptance_ok = not acceptance_required or (acceptance_passed is True and acceptance_error is None)
    passed = (
        would_pass
        and adequacy_implemented is not False
        and adequacy_tests_assert is not False
        and adequacy_error is None
        and acceptance_ok
    )
    if require_test_changes and not touched:
        passed = False
    # #508 : signal par défaut, bloquant seulement en opt-in. Appliqué APRÈS le
    # calcul de `passed` (n'entre pas dans would_pass : ne change pas l'économie
    # d'appels d'adéquation #437).
    forbidden_blocked = bool(forbidden_files_block and forbidden_files)
    if forbidden_blocked:
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
        requirements_added=tuple(requirements_added),
        requirements_removed=requirements_removed,
        requirements_unpinned=requirements_unpinned,
        adequacy_tests_assert=adequacy_tests_assert,
        adequacy_tests_justification=adequacy_tests_justification,
        forbidden_files=forbidden_files,
        forbidden_files_blocking=forbidden_blocked,
        acceptance_passed=acceptance_passed,
        acceptance_output=acceptance_output,
        acceptance_error=acceptance_error,
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


# #500 : un diff qui touche de l'AUTH (routes d'auth, « utilisateur courant ») est
# le terrain de l'IDOR — la fuite cross-user la plus probable d'une app CRUD
# générée par LLM (run v5 : clients lisibles/modifiables par tout compte). La
# détection vit sur le DIFF (pas la config) → active au run réel même si le
# harness bypasse _gate_options.
_AUTH_DIFF_MARKERS = (
    "get_current_user",
    "current_user",
    "oauth2passwordbearer",
    "/auth/",
    "httpbearer",
    "import jwt",
    "from jwt",
    "jwt.encode",
    "jwt.decode",
    "depends(get_current",
)

# Consigne de revue ciblée, injectée quand le diff touche l'auth. Heuristique
# best-effort : elle peut RATER une auth maison sans marqueur idiomatique (le
# standard `security` mentionne alors l'IDOR de façon inconditionnelle comme
# filet) ; un faux positif ne coûte qu'une consigne en trop — on erre du bon
# côté en sécurité.
_OWNERSHIP_REVIEW_CONSIGNE = (
    "REVUE SÉCURITÉ — ISOLATION PAR PROPRIÉTAIRE (IDOR) : cette app a de "
    "l'authentification. Toute ressource créée par un utilisateur DOIT être "
    "filtrée par son propriétaire en lecture, écriture ET suppression. Signale "
    "en `critical` (catégorie `security`) tout endpoint CRUD (GET/PUT/PATCH/"
    "DELETE d'une ressource par id) qui n'applique aucun filtre owner — un "
    "autre utilisateur authentifié pourrait y accéder."
)


def _diff_touches_auth(diff: str) -> bool:
    """Le diff introduit-il de l'authentification ? (heuristique, insensible casse, #500)."""
    haystack = (diff or "").lower()
    return any(marker in haystack for marker in _AUTH_DIFF_MARKERS)


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

    def __init__(
        self,
        *,
        min_quality: float = DEFAULT_MIN_QUALITY,
        tool=None,
        review_context: str = "",
        ownership_review: bool = True,
    ):
        self._min_quality = min_quality
        self._tool = tool  # injectable pour les tests ; sinon construit à la volée
        # Calibration de la revue au PROJET (générique). ``review_context`` : consigne
        # libre injectée dans le prompt du reviewer (ex. « prototype, auth différée P2 :
        # ne bloque pas sur l'absence d'auth/IDOR, l'isolation par projet suffit »).
        # ``ownership_review`` : injecter (ou non) la consigne IDOR auto sur diff
        # touchant l'auth (#500) — à désactiver pour un projet où l'auth est différée.
        self._review_context = str(review_context or "")
        self._ownership_review = bool(ownership_review)

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
        base_context = issue.to_prompt() if issue is not None else None
        # #500 : si le diff touche l'auth, injecter la consigne ownership/IDOR — sauf si
        # la revue ownership est désactivée pour ce projet (auth différée, prototype).
        if self._ownership_review and _diff_touches_auth(diff):
            base_context = (
                f"{base_context}\n\n{_OWNERSHIP_REVIEW_CONSIGNE}" if base_context else _OWNERSHIP_REVIEW_CONSIGNE
            )
        # Contexte de revue PAR PROJET (calibration au stade de maturité) : appended au
        # prompt du reviewer pour qu'il juge au bon niveau (ne pas exiger des features
        # explicitement différées par la spec). Générique, vide par défaut.
        if self._review_context:
            base_context = f"{base_context}\n\n{self._review_context}" if base_context else self._review_context
        request = CodeReviewRequest(code=diff or "(diff vide)", language=language, context=base_context)
        response = await tool.execute_async(request, ctx=ctx)
        return outcome_from_review(response, min_quality=self._min_quality)

    @staticmethod
    def _build_tool():  # pragma: no cover - chemin réel (integration)
        from collegue.tools.code_review.tool import CodeReviewTool

        return CodeReviewTool()
