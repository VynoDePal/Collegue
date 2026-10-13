"""Support des tests adverses de la paire JSON (lot B37) : faux Google natif, processus enfants, relectures indépendantes du registre.

Rien ici ne décide d'un verdict : le faux Google ne fait que répondre et JOURNALISER ce qu'il reçoit (la vérité terrain des émissions,
partagée entre processus), et les relectures SQLite n'utilisent que les tables EXISTANTES du registre budgétaire (jamais le journal privé du
lanceur de A). Le vrai ``GoogleUpstream`` (hôte, chemins, en-tête de clé, plafond de réponse) est conservé ; seule la couche réseau est
remplacée par ``httpx.MockTransport``. Les comportements sont des dictionnaires JSON, donc transmissibles à un processus enfant : un crash
RÉEL (``os._exit``), une concurrence RÉELLE entre processus et une reprise par un processus neuf sont ainsi possibles.

Exécutable comme enfant : ``python tests/w5_json_pair_support.py child <spec.json>`` (avec la racine du dépôt dans ``PYTHONPATH``).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── constantes du contrat ────────────────────────────────────────────────────────────────────────────────────────────────

MODEL = "gemma-4-31b-it"
FAKE_KEY = "AIzaSyB37-pair-FAKE-key-0001"
HOSTILE_KEY = "AIzaSyB37-hostile-FAKE-key-0002"
HOSTILE_URL = "https://attacker.example/steal?token=B37URLTOKEN"
HOSTILE_TEXT = "B37-HOSTILE-FRAGMENT-9f3a"
OPAQUE_ID = "Rq7Xk29LmN4pQ8sT-b37"
SENTINELS = (FAKE_KEY, HOSTILE_KEY, HOSTILE_URL, "attacker.example", "B37URLTOKEN", HOSTILE_TEXT, OPAQUE_ID)

PROMPT = 'Réponds uniquement avec l\'objet JSON {"ok": true}.'
SHA_PROMPT_ONLY = "2edf377188f9ee909f3a349d5443810f437ac4068d930cf5708430ee8badecc2"
SHA_NATIVE_JSON = "98d8c3444dcbe29c66bac30e5fc631c7ecff87aec5f2dfe57958aba1a4d3a300"
EXPECTED_SHA = {"prompt_only": SHA_PROMPT_ONLY, "native_json": SHA_NATIVE_JSON}
VARIANTS = ("prompt_only", "native_json")
COUNTED_TOKENS = 14
OUTPUT_CAP = 256
RESERVED = COUNTED_TOKENS + OUTPUT_CAP  # 270, comme dans la campagne historique

EXPECTED_GENERATE = {
    "prompt_only": {
        "model": f"models/{MODEL}",
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {"candidateCount": 1, "maxOutputTokens": OUTPUT_CAP},
    },
    "native_json": {
        "model": f"models/{MODEL}",
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {
            "candidateCount": 1,
            "maxOutputTokens": OUTPUT_CAP,
            "responseMimeType": "application/json",
        },
    },
}
EXPECTED_CHAT = {
    "prompt_only": {"model": MODEL, "max_tokens": OUTPUT_CAP, "messages": [{"role": "user", "content": PROMPT}]},
    "native_json": {
        "model": MODEL,
        "max_tokens": OUTPUT_CAP,
        "messages": [{"role": "user", "content": PROMPT}],
        "response_format": {"type": "json_object"},
    },
}
T0 = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def canonical_sha(body: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


# ── comportements du faux Google (dictionnaires JSON) ────────────────────────────────────────────────────────────────────


def ok(text: Optional[str] = '{"ok": true}', usage=(COUNTED_TOKENS, 8, 15), finish: str = "STOP") -> Dict[str, Any]:
    return {"k": "ok", "text": text, "usage": list(usage) if usage is not None else None, "finish": finish}


def ok_invalid_usage(kind: str) -> Dict[str, Any]:
    """200 dont l'usage est contradictoire ou dément une borne : ``missing`` / ``inconsistent`` / ``over_count`` / ``over_cap``."""
    return {"k": "ok_usage", "kind": kind}


def http(status: int, rpc: str = "INTERNAL", hostile: bool = True) -> Dict[str, Any]:
    return {"k": "http", "status": status, "rpc": rpc, "hostile": hostile}


def raises(kind: str) -> Dict[str, Any]:
    """``connect`` (rien n'est parti), ``read_timeout`` (envoyé puis perdu), ``runtime`` (exception arbitraire à texte hostile)."""
    return {"k": "raise", "exc": kind}


def raw(content: str, status: int = 200) -> Dict[str, Any]:
    return {"k": "raw", "status": status, "content": content}


def crash(code: int = 137) -> Dict[str, Any]:
    """Le processus meurt à CET instant, après avoir reçu la requête et avant toute réponse (``os._exit``)."""
    return {"k": "crash", "code": code}


def sleep(seconds: float, then: Dict[str, Any]) -> Dict[str, Any]:
    return {"k": "sleep", "seconds": seconds, "then": then}


def block_until(marker: str, release: str, then: Dict[str, Any]) -> Dict[str, Any]:
    """Écrit ``marker`` puis attend l'existence de ``release`` (jamais créé : le test tue le processus de l'extérieur)."""
    return {"k": "block", "marker": marker, "release": release, "then": then}


def advance_clock(seconds: float, then: Dict[str, Any]) -> Dict[str, Any]:
    """Simule une réponse lente : l'horloge partagée (fichier) avance de ``seconds`` avant la réponse."""
    return {"k": "advance", "seconds": seconds, "then": then}


def script(prompt_only=None, native_json=None, *, count_prompt_only=None, count_native_json=None) -> Dict[str, Any]:
    return {
        "prompt_only": {"count": count_prompt_only or {"k": "count"}, "generate": prompt_only or ok()},
        "native_json": {"count": count_native_json or {"k": "count"}, "generate": native_json or ok()},
    }


class FileClock:
    """Horloge UTC injectable et PARTAGEABLE entre processus : ``T0`` + un décalage lu dans un fichier."""

    def __init__(self, offset_file: Path):
        self.offset_file = Path(offset_file)

    def __call__(self) -> datetime:
        try:
            offset = float(self.offset_file.read_text().strip() or 0)
        except (OSError, ValueError):
            offset = 0.0
        return T0 + timedelta(seconds=offset)

    def advance(self, seconds: float) -> None:
        current = 0.0
        try:
            current = float(self.offset_file.read_text().strip() or 0)
        except (OSError, ValueError):
            pass
        self.offset_file.write_text(str(current + seconds))


# ── faux Google natif ────────────────────────────────────────────────────────────────────────────────────────────────────


def _google_reply(text: Optional[str], usage, finish: str) -> Dict[str, Any]:
    parts = [{"text": text}] if text is not None else []
    body: Dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}
    if usage is not None:
        prompt, cand, thoughts = usage
        body["usageMetadata"] = {
            "promptTokenCount": prompt,
            "candidatesTokenCount": cand,
            "thoughtsTokenCount": thoughts,
            "totalTokenCount": prompt + cand + thoughts,
        }
    return body


def hostile_error_body(status: int, rpc: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": status,
            "status": rpc,
            "message": f"{HOSTILE_TEXT} key={HOSTILE_KEY} configured={FAKE_KEY} see {HOSTILE_URL}\r\nINJECTED",
            "details": [
                {"detail": HOSTILE_TEXT, "metadata": {"requestId": OPAQUE_ID, "nested": [{"deep": HOSTILE_KEY}]}}
            ],
        }
    }


class Wire:
    """Faux Google : répond selon le script et journalise, dans un fichier PARTAGÉ (O_APPEND), chaque requête reçue.

    ``peek_database`` : à chaque requête, relit (lecture seule) l'état du registre — existence de l'horloge globale, tentatives
    ``emitting``, jetons réservés — pour prouver l'ORDRE (échéance ouverte avant le premier comptage, réservation avant génération)."""

    def __init__(
        self,
        log: Path,
        script_: Dict[str, Any],
        *,
        peek_database: Optional[Path] = None,
        clock_file: Optional[Path] = None,
    ):
        self.log = Path(log)
        self.script = script_
        self.peek_database = Path(peek_database) if peek_database else None
        self.clock_file = Path(clock_file) if clock_file else None

    # ------------------------------------------------------------------ journal (vérité terrain)
    def _append(self, entry: Dict[str, Any]) -> None:
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def events(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def of(self, op: str, variant: Optional[str] = None) -> List[Dict[str, Any]]:
        return [e for e in self.events() if e["op"] == op and (variant is None or e["variant"] == variant)]

    def generated(self) -> List[Dict[str, Any]]:
        return self.of("generateContent")

    # ------------------------------------------------------------------ registre (lecture seule, tables existantes)
    def _peek(self) -> Optional[Dict[str, Any]]:
        if self.peek_database is None:
            return None
        try:
            conn = sqlite3.connect(f"file:{self.peek_database}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            return None
        try:
            return {
                "clock_rows": conn.execute("select count(*) from broker_clocks").fetchone()[0],
                "emitting_attempts": conn.execute(
                    "select count(*) from broker_attempts where state='emitting'"
                ).fetchone()[0],
                "reserved_tokens": conn.execute(
                    "select coalesce(sum(reserved_tokens),0) from budget_scopes"
                ).fetchone()[0],
                "attempts": conn.execute("select count(*) from broker_attempts").fetchone()[0],
            }
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    # ------------------------------------------------------------------ transport
    def handler(self, request):
        import httpx

        op = request.url.path.rsplit(":", 1)[-1]
        url_model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        body = json.loads(request.content)
        content = body["generateContentRequest"] if op == "countTokens" else body
        variant = (
            "native_json"
            if content.get("generationConfig", {}).get("responseMimeType") == "application/json"
            else "prompt_only"
        )
        entry: Dict[str, Any] = {
            "pid": os.getpid(),
            "t": time.time(),
            "op": op,
            "variant": variant,
            "model": url_model,
            "host": request.url.host,
            "key_header_ok": request.headers.get("x-goog-api-key") == FAKE_KEY,
            "key_in_url": FAKE_KEY in str(request.url),
            "body_is_expected": content == EXPECTED_GENERATE[variant],
            "body_sha": canonical_sha(content),
            "peek": self._peek(),
        }
        if op == "countTokens":
            self._append(entry)
            behavior = self.script[variant]["count"]
            return self._respond(behavior, httpx, count=True)
        self._append(entry)
        return self._respond(self.script[variant]["generate"], httpx)

    def _respond(self, behavior: Dict[str, Any], httpx, *, count: bool = False):
        kind = behavior["k"]
        if kind == "count":
            return httpx.Response(200, json={"totalTokens": behavior.get("tokens", COUNTED_TOKENS)})
        if kind == "ok":
            return httpx.Response(200, json=_google_reply(behavior["text"], behavior["usage"], behavior["finish"]))
        if kind == "ok_usage":
            usage_kind = behavior["kind"]
            if usage_kind == "missing":
                return httpx.Response(200, json=_google_reply('{"ok": true}', None, "STOP"))
            if usage_kind == "inconsistent":
                reply = _google_reply('{"ok": true}', (COUNTED_TOKENS, 8, 15), "STOP")
                reply["usageMetadata"]["totalTokenCount"] += 99
                return httpx.Response(200, json=reply)
            if usage_kind == "over_count":
                return httpx.Response(200, json=_google_reply('{"ok": true}', (COUNTED_TOKENS + 50, 8, 15), "STOP"))
            if usage_kind == "over_cap":
                return httpx.Response(200, json=_google_reply('{"ok": true}', (COUNTED_TOKENS, 200, 200), "STOP"))
            raise AssertionError(usage_kind)
        if kind == "http":
            body = (
                hostile_error_body(behavior["status"], behavior["rpc"])
                if behavior["hostile"]
                else {"error": {"code": behavior["status"]}}
            )
            return httpx.Response(
                behavior["status"],
                json=body,
                headers={
                    "x-goog-request-id": OPAQUE_ID,
                    "x-echo-key": FAKE_KEY,
                    "www-authenticate": f'Bearer realm="{HOSTILE_URL}"',
                },
            )
        if kind == "raise":
            if behavior["exc"] == "connect":
                raise httpx.ConnectError("refused")
            if behavior["exc"] == "read_timeout":
                raise httpx.ReadTimeout("slow")
            raise RuntimeError(f"{HOSTILE_TEXT} {HOSTILE_KEY} {HOSTILE_URL}")
        if kind == "raw":
            return httpx.Response(behavior["status"], content=behavior["content"].encode())
        if kind == "crash":
            os._exit(behavior["code"])
        if kind == "sleep":
            time.sleep(behavior["seconds"])
            return self._respond(behavior["then"], httpx)
        if kind == "block":
            Path(behavior["marker"]).write_text(str(os.getpid()))
            deadline = time.time() + 120
            while not Path(behavior["release"]).exists() and time.time() < deadline:
                time.sleep(0.02)
            return self._respond(behavior["then"], httpx)
        if kind == "advance":
            FileClock(self.clock_file).advance(behavior["seconds"])
            return self._respond(behavior["then"], httpx)
        raise AssertionError(kind)

    def upstream(self):
        import httpx

        from collegue.broker.upstream import GoogleUpstream

        return GoogleUpstream(lambda: FAKE_KEY, timeout=5.0, transport=httpx.MockTransport(self.handler))


# ── relectures indépendantes du registre (tables existantes seulement) ───────────────────────────────────────────────────


def registry_totals(database: Path) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    try:
        consumed, reserved, unknown = conn.execute(
            "select coalesce(sum(consumed_tokens),0), coalesce(sum(reserved_tokens),0), coalesce(sum(unknown_tokens),0) from budget_scopes"
        ).fetchone()
        blocked = conn.execute("select count(*) from budget_scopes where blocked_reason is not null").fetchone()[0]
        scopes = conn.execute("select count(*) from budget_scopes").fetchone()[0]
        attempts = conn.execute(
            "select request_id, state, counted_tokens, reserved_tokens from broker_attempts order by id"
        ).fetchall()
        clocks = conn.execute("select scope_key, seconds, opened_at, deadline_at from broker_clocks").fetchall()
        caps = conn.execute("select cap_micro_usd, cap_tokens from budget_scopes").fetchall()
        return {
            "consumed": consumed,
            "reserved": reserved,
            "unknown": unknown,
            "blocked": bool(blocked),
            "scopes": scopes,
            "attempts": [tuple(a) for a in attempts],
            "clocks": [tuple(c) for c in clocks],
            "caps": [tuple(c) for c in caps],
        }
    finally:
        conn.close()


def registry_dump(
    database: Path, tables=("budget_scopes", "budget_reservations", "budget_events", "broker_attempts", "broker_clocks")
) -> Dict[str, Any]:
    """Contenu sémantique des tables du registre (pour prouver qu'une lecture ou un refus ne modifie rien)."""
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    try:
        return {t: [tuple(r) for r in conn.execute(f"select * from {t} order by 1")] for t in tables}
    finally:
        conn.close()


def leaked(value: Any) -> List[str]:
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    return [s for s in SENTINELS if s in text]


def database_bytes(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(Path(directory).rglob("*")) if p.is_file())


# ── processus enfants ────────────────────────────────────────────────────────────────────────────────────────────────────


REPO = Path(__file__).resolve().parents[1]


def child_spec(
    *,
    database: Path,
    campaign_id: str,
    wire: Wire,
    clock_file: Optional[Path] = None,
    barrier: Optional[Path] = None,
    tag: str = "child",
) -> Dict[str, Any]:
    return {
        "database": str(database),
        "campaign_id": campaign_id,
        "log": str(wire.log),
        "script": wire.script,
        "peek": str(wire.peek_database) if wire.peek_database else None,
        "clock_file": str(clock_file) if clock_file else None,
        "barrier": str(barrier) if barrier else None,
        "tag": tag,
    }


def spawn(spec: Dict[str, Any], directory: Path, name: str) -> subprocess.Popen:
    spec_file = Path(directory) / f"spec-{name}.json"
    spec_file.write_text(json.dumps(spec))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in list(env):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET")):
            env.pop(key)
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "child", str(spec_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(directory),
        text=True,
    )


def finish(process: subprocess.Popen, timeout: float = 90.0) -> Dict[str, Any]:
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        return {"rc": "timeout", "result": None, "stdout": out, "stderr": err}
    result = None
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT "):
            result = json.loads(line[len("RESULT ") :])
            break
    return {"rc": process.returncode, "result": result, "stdout": out, "stderr": err}


def run_child(spec: Dict[str, Any], directory: Path, name: str, timeout: float = 90.0) -> Dict[str, Any]:
    return finish(spawn(spec, directory, name), timeout)


def child_main(spec_file: str) -> None:
    spec = json.loads(Path(spec_file).read_text())
    from collegue.broker import json_pair

    wire = Wire(
        Path(spec["log"]),
        spec["script"],
        peek_database=Path(spec["peek"]) if spec["peek"] else None,
        clock_file=Path(spec["clock_file"]) if spec["clock_file"] else None,
    )
    clock = FileClock(Path(spec["clock_file"])) if spec["clock_file"] else None
    barrier = spec["barrier"]
    if barrier:
        Path(f"{barrier}.ready.{os.getpid()}").write_text("ready")
        deadline = time.time() + 60
        while not Path(barrier).exists() and time.time() < deadline:
            time.sleep(0.005)
    try:
        report = asyncio.run(
            json_pair.run_pair(
                database=Path(spec["database"]), campaign_id=spec["campaign_id"], upstream=wire.upstream(), clock=clock
            )
        )
        result = {"outcome": "returned", "report": report}
    except BaseException as exc:  # noqa: BLE001 - l'issue (type, texte) est une observation
        result = {"outcome": "raised", "type": type(exc).__name__, "text": str(exc)[:300], "repr": repr(exc)[:300]}
    print("RESULT " + json.dumps(result, default=repr))


async def outcome(coroutine) -> Dict[str, Any]:
    """Issue observable d'un appel : ``returned`` (avec son dictionnaire) ou ``raised`` (type et texte)."""
    try:
        return {"outcome": "returned", "report": await coroutine}
    except BaseException as exc:  # noqa: BLE001
        return {"outcome": "raised", "type": type(exc).__name__, "text": str(exc), "repr": repr(exc)}


def copy_payloads(payloads) -> Any:
    return copy.deepcopy(payloads)


if __name__ == "__main__":  # pragma: no cover - exécuté comme processus enfant
    if len(sys.argv) == 3 and sys.argv[1] == "child":
        child_main(sys.argv[2])
    else:
        raise SystemExit("usage : w5_json_pair_support.py child <spec.json>")
