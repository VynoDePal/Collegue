"""Harnais de la PREUVE COMPOSÉE W5 (propriété C) : SDK réel dans l'image broker, VRAI courtier hôte, faux fournisseur Google derrière.

Ce que cette preuve compose (exécutée par le job « Docker build » après intégration de A et B ; jamais en local, jamais sans Docker) :

* **conteneur réel** : l'image finale ``collegue-sandbox-broker`` (vrai SDK OpenHands, vrai ``/opt/oh_runner.py``, vrai relais
  ``/opt/oh_broker_relay.py``), lancée par le VRAI ``DockerSandbox`` (``--network none``, un seul montage du courtier en lecture seule) ;
* **hôte de confiance réel** : ``BrokerRuntime`` + ``BrokerService`` + ``BrokerSocketServer`` + registre budgétaire SQLite réels ;
* **faux fournisseur Google** (``FakeGoogle``) branché DERRIÈRE le service (seul point injectable, comme pour les tests de A) : il observe
  ``countTokens`` et ``generateContent`` (objet exact reçu) et rejoue un scénario ; le SDK ne lui parle jamais directement.

Les mesures d'usage viennent du REGISTRE (``ledger.snapshot``), jamais de la sortie du worker. Les interfaces de A utilisées ici sont
listées dans ``INTERFACE_CONTRACT`` : si A les change, cette preuve échoue (elle ne s'adapte pas à l'aveugle) et le défaut revient à A.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

IMAGE = os.environ.get("W5_BROKER_IMAGE", "collegue-sandbox-broker:pr-check")
PROVIDER_KEY = "AIzaFAKE-w5-composed-provider-key-0001"  # factice : prouvé absent de TOUT sauf du fournisseur simulé

#: Surface de A dont cette preuve dépend (relue à l'intégration ; un écart = échec, pas adaptation silencieuse).
INTERFACE_CONTRACT = {
    "collegue.broker.BrokerConfig(global_deadline_seconds, close_wait_seconds)": "échéance globale persistée, attente de fermeture",
    "collegue.broker.runtime.BrokerRuntime(upstream, config, run_root, provider_keys)": "service de confiance ; faux fournisseur injectable",
    "BrokerRuntime.service_for(ledger).open_clock(scope_key) / persisted_deadline(scope_key)": "échéance ouverte AVANT la planification",
    "BrokerService.store.open_sessions()": "sessions encore ouvertes (aucune ne doit rester)",
    "collegue.executor.openhands_sdk_agent.OHSdkAgent(sandbox, settings_obj, broker, python_bin, runner_path)": "worker raccordé",
    "collegue.executor.runner._run_agent_under_budget(agent, workspace, IssueSpec)": "chemin public d'un worker sous allocation",
    "collegue.pilot.runtime._coder_sandbox_kwargs(settings)": "câblage de production du sandbox du codeur en mode courtier",
    "collegue.core.llm.budget_guard.bind_budget(ledger, scope, settings, deadline)": "liaison du registre",
    "ledger.snapshot(scope).{consumed,reserved,unknown}_tokens / .blocked": "mesures d'usage (autorité du registre)",
}


class DockerUnavailable(AssertionError):
    """Docker ou l'image manque : dans le job qui exige cette preuve c'est un ÉCHEC, jamais un saut."""


def require_docker_and_image(image: str = IMAGE) -> None:
    docker = shutil.which("docker")
    if not docker:
        raise DockerUnavailable("docker introuvable : la preuve composée exige Docker")
    done = subprocess.run([docker, "image", "inspect", image], capture_output=True, text=True)
    if done.returncode != 0:
        raise DockerUnavailable(f"image {image!r} absente : construire docker/sandbox/Dockerfile.broker d'abord")


def containers_of(image: str = IMAGE) -> List[str]:
    """Identifiants des conteneurs (vivants OU arrêtés) créés depuis l'image : doit être vide après un worker."""
    done = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"ancestor={image}"], capture_output=True, text=True, check=True
    )
    return [line for line in done.stdout.split() if line]


# ── faux fournisseur Google (derrière le VRAI service) ───────────────────────────────────────────────────────────────────


def google_response(
    text: str = "ok",
    *,
    prompt: int = 10,
    candidates: int = 5,
    parts: Optional[list] = None,
) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": candidates,
            "totalTokenCount": prompt + candidates,
        },
    }


class FakeGoogle:
    """Fournisseur injectable : enregistre l'objet EXACT reçu par ``countTokens`` et ``generateContent`` et rejoue un scénario.

    ``gate`` : si présent, ``generate`` n'a jamais de réponse (requête émise, worker bloqué en vol). ``script`` : ``callable(body) -> dict``
    choisit la réponse selon la requête normalisée (outils déclarés, historique)."""

    PROMPT, CANDIDATES = 10, 5

    def __init__(self, *, script=None):
        self.count_calls: List[dict] = []
        self.generate_calls: List[dict] = []
        self.gate: Optional[asyncio.Event] = None
        self.script = script

    async def count_tokens(self, request) -> dict:
        self.count_calls.append({"model": request.model, "body": copy.deepcopy(request.count_tokens_body())})
        return {"totalTokens": self.PROMPT}

    async def generate(self, request) -> dict:
        body = copy.deepcopy(request.generate_body())
        self.generate_calls.append({"model": request.model, "body": body})
        if self.gate is not None:
            await self.gate.wait()
        if self.script is not None:
            return self.script(body)
        return google_response("ok", prompt=self.PROMPT, candidates=self.CANDIDATES)


def declared_functions(body: dict) -> Dict[str, dict]:
    """Fonctions déclarées dans la requête normalisée (``tools[*].functionDeclarations``), par nom."""
    declared: Dict[str, dict] = {}
    for tool in body.get("tools", []) or []:
        for declaration in tool.get("functionDeclarations", []) or []:
            declared[str(declaration.get("name"))] = declaration
    return declared


def has_function_response(body: dict) -> bool:
    return any("functionResponse" in part for content in body.get("contents", []) for part in content.get("parts", []))


def sdk_script(marker_file: str):
    """Scénario du VRAI runner OpenHands : un appel d'outil ``terminal`` qui écrit un fichier, puis ``finish``.

    Le choix se fait sur la requête NORMALISÉE reçue par le fournisseur (outils déclarés, présence d'un résultat d'outil) : si le SDK
    n'émet pas ces outils, la preuve échoue (champ ou outil non pris en charge = défaut à renvoyer à A, jamais assoupli ici)."""

    def respond(body: dict) -> dict:
        functions = declared_functions(body)
        if "terminal" not in functions or "finish" not in functions:
            raise AssertionError(f"outils OpenHands absents de la requête normalisée : {sorted(functions)}")
        if not has_function_response(body):
            call = {"functionCall": {"name": "terminal", "args": {"command": f"echo composed-proof > {marker_file}"}}}
        else:
            call = {"functionCall": {"name": "finish", "args": {"message": "done"}}}
        return google_response(parts=[call], prompt=FakeGoogle.PROMPT, candidates=FakeGoogle.CANDIDATES)

    return respond


# ── pile hôte réelle ─────────────────────────────────────────────────────────────────────────────────────────────────────

SETTINGS = dict(
    LLM_PROVIDER="gemini",
    LLM_MODEL="gemma-4-31b-it",
    LLM_API_KEY=PROVIDER_KEY,
    LLM_TRANSPORT="budget_broker",
    CODER_SUBSCRIPTION=False,
    COLLEGUE_RUN_DEADLINE_SECONDS=0.0,
    BUDGET_WORKER_SHARE=0.5,
    CODER_FALLBACK_MODELS="gemma-4-26b-a4b-it",
)


class HostStack:
    """Registre + courtier + sandbox Docker réel + agent, comme le pilote les assemble en production."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        upstream: FakeGoogle,
        deadline: int = 900,
        close_wait: float = 1.0,
        worker_source: Optional[str] = None,
        image: str = IMAGE,
        extra_env: Optional[Dict[str, str]] = None,
    ):
        from collegue.broker import BrokerConfig
        from collegue.broker.runtime import BrokerRuntime

        from collegue.executor.openhands_sdk_agent import OHSdkAgent
        from collegue.pilot import runtime as pilot_runtime
        from collegue.sandbox.executor import DockerSandbox
        from collegue.state import ProjectStateManager

        self.tmp_path = tmp_path
        self.upstream = upstream
        self.settings = SimpleNamespace(**{**SETTINGS, "SANDBOX_IMAGE": image})
        self.manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)
        self.ledger = self.manager.budget_ledger
        self.scope = self.ledger.scope_for_project(
            self.manager.create_project(name="w5-composed"), max_cost_usd=2.0, max_tokens=250_000
        ).scope_key
        run_root = tempfile.mkdtemp(prefix="cbk-", dir="/tmp")  # racine COURTE (socket AF_UNIX ≤ 108 octets)
        self.run_root = run_root
        self.runtime = BrokerRuntime(
            upstream=upstream,
            config=BrokerConfig(global_deadline_seconds=deadline, close_wait_seconds=close_wait),
            run_root=run_root,
            provider_keys=lambda: (PROVIDER_KEY,),
        )
        self.service = self.runtime.service_for(self.ledger)
        kwargs = pilot_runtime._coder_sandbox_kwargs(self.settings)  # câblage de PRODUCTION du codeur en mode courtier
        relay_path = os.environ.get(
            "W5_RELAY_PATH", "/opt/oh_broker_relay.py"
        )  # le relais EMBARQUÉ dans l'image (défaut)
        kwargs["env"] = {**kwargs["env"], "W5_RELAY_PATH": relay_path, **(extra_env or {})}
        self.sandbox = DockerSandbox(workspace_root=str(tmp_path), **kwargs)
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        runner = "/opt/oh_runner.py"
        if worker_source is not None:
            (self.workspace / "worker.py").write_text(textwrap.dedent(worker_source), encoding="utf-8")
            runner = "/workspace/worker.py"
        self.agent = OHSdkAgent(
            self.sandbox, settings_obj=self.settings, broker=self.runtime, python_bin="python", runner_path=runner
        )

    def binding(self, *, window: int = 3600):
        from collegue.core.llm.budget_guard import bind_budget

        return bind_budget(
            self.ledger,
            self.scope,
            settings=self.settings,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=window),
        )

    def run(self, *, window: int = 3600):
        from collegue.executor.agent import IssueSpec
        from collegue.executor.runner import _run_agent_under_budget

        with self.binding(window=window):
            return _run_agent_under_budget(
                self.agent, str(self.workspace), IssueSpec(number=1, title="preuve composée", body="écrire le fichier")
            )

    def close(self) -> None:
        shutil.rmtree(self.run_root, ignore_errors=True)


# ── worker de la preuve d'échéance (même comportements que la preuve hôte de A, mais DANS le conteneur réel) ──────────────────

WORKER = """\
    import importlib.util, os, sys, time
    import openai
    spec = importlib.util.spec_from_file_location("oh_broker_relay", os.environ["W5_RELAY_PATH"])
    relay = importlib.util.module_from_spec(spec); spec.loader.exec_module(relay)
    _server, port = relay.start(os.environ["COLLEGUE_BROKER_SOCKET"])
    client = openai.OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key=os.environ["LLM_API_KEY"], max_retries=0, timeout=300)
    behaviour = os.environ.get("W5_BEHAVIOUR", "quick")
    def call():
        client.chat.completions.create(model="openai/gemma-4-31b-it", messages=[{"role": "user", "content": "x"}], max_tokens=64)
    if behaviour == "sleep_only":
        time.sleep(600)
    call()
    if behaviour == "sleep_after_call":
        time.sleep(600)
    elif behaviour == "compute_after_call":
        while True:
            pass
    print("WORKER_DONE")
"""

#: Code HOSTILE exécuté dans le conteneur : il appelle le socket du courtier avec ce qu'il veut, sans passer par le SDK, puis sonde son isolation.
HOSTILE = """\
    import json, os, socket, sys
    facts = {}
    sock_path = os.environ["COLLEGUE_BROKER_SOCKET"]
    token = os.environ["LLM_API_KEY"]

    def post(path, body, *, bearer=token, extra_headers=(), absolute=False):
        data = json.dumps(body).encode()
        target = ("http://example.com" + path) if absolute else path
        head = [f"POST {target} HTTP/1.1", "Host: x", f"Content-Length: {len(data)}", "Content-Type: application/json"]
        if bearer is not None:
            head.append(f"Authorization: Bearer {bearer}")
        head += list(extra_headers)
        raw = ("\\r\\n".join(head) + "\\r\\n\\r\\n").encode() + data
        s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(sock_path); s.sendall(raw)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        s.close()
        return int(out.split(b" ", 2)[1]) if out else 0

    good = {"model": "gemma-4-31b-it", "messages": [{"role": "user", "content": "x"}], "max_tokens": 64}
    facts["unauthorized_model"] = post("/v1/chat/completions", {**good, "model": "gemini-2.5-pro"})
    facts["unknown_session_token"] = post("/v1/chat/completions", good, bearer="cbk_0000000000000000000000000000000000000000")
    facts["no_token"] = post("/v1/chat/completions", good, bearer=None)
    facts["other_route"] = post("/v1/models", {})
    facts["administration_route"] = post("/admin/sessions", {})
    facts["absolute_url_request_line"] = post("/v1/chat/completions", good, absolute=True)
    facts["injected_base_url"] = post("/v1/chat/completions", {**good, "base_url": "https://example.com/v1"})
    facts["injected_api_key_field"] = post("/v1/chat/completions", {**good, "api_key": "AIzaSomethingElse"})
    facts["injected_scope"] = post("/v1/chat/completions", {**good, "scope": "global", "role": "planner"})
    facts["provider_key_header"] = post("/v1/chat/completions", good, extra_headers=["x-goog-api-key: AIzaSomethingElse"])
    facts["stream"] = post("/v1/chat/completions", {**good, "stream": True})
    facts["hosted_tool"] = post("/v1/chat/completions", {**good, "tools": [{"type": "web_search"}]})
    facts["control_legitimate_call"] = post("/v1/chat/completions", good)

    external = []
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("142.250.74.202", 443), ("generativelanguage.googleapis.com", 443)):
        try:
            socket.create_connection((host, port), timeout=3).close()
            external.append(f"{host}:{port}")
        except OSError:
            pass
    facts["external_connections"] = external
    with open("/proc/net/dev") as handle:
        facts["interfaces"] = sorted(line.split(":")[0].strip() for line in handle.read().splitlines()[2:] if ":" in line)
    facts["secret_like_env"] = sorted(k for k in os.environ if any(w in k.upper() for w in ("API_KEY", "SECRET", "PASSWORD", "GITHUB", "GOOGLE", "GEMINI")))
    facts["env_has_provider_key_value"] = any("AIzaFAKE" in v for v in os.environ.values())
    facts["llm_api_key_is_a_session_token"] = token.startswith("cbk_")
    with open("/proc/mounts") as handle:
        facts["mounts"] = sorted({line.split()[1] for line in handle if line.split()[1].startswith(("/run", "/workspace", "/var", "/home", "/host"))})
    facts["docker_socket_visible"] = os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock")
    print("HOSTILE_FACTS=" + json.dumps(facts))
"""


def parse_facts(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("HOSTILE_FACTS="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError("le code hostile n'a rien rapporté : " + stdout[-500:])
