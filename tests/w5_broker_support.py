"""Doubles de test du courtier W5 : faux fournisseur Google qui COMPTE les émissions (jamais de réseau, jamais de clé)."""

from __future__ import annotations

import asyncio
import copy
from typing import Callable, List, Optional

from collegue.broker.upstream import UpstreamBadBody, UpstreamHTTPError, UpstreamTransportError


def google_response(
    text: str = "ok",
    *,
    prompt: int = 10,
    candidates: int = 5,
    thoughts: int = 0,
    tool_use: int = 0,
    total: Optional[int] = None,
    finish: str = "STOP",
    parts: Optional[list] = None,
    drop_usage: bool = False,
) -> dict:
    body = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]},
                "finishReason": finish,
            }
        ]
    }
    if not drop_usage:
        usage = {
            "promptTokenCount": prompt,
            "totalTokenCount": total if total is not None else prompt + tool_use + candidates + thoughts,
        }
        if candidates:
            usage["candidatesTokenCount"] = candidates
        if thoughts:
            usage["thoughtsTokenCount"] = thoughts
        if tool_use:
            usage["toolUsePromptTokenCount"] = tool_use
        body["usageMetadata"] = usage
    return body


class FakeUpstream:
    """Fournisseur injectable : enregistre chaque countTokens / generate (objet exact reçu) et rejoue un scénario."""

    def __init__(self, *, count: int = 10, response: Optional[dict] = None):
        self.count = count
        self.response = response if response is not None else google_response()
        self.count_calls: List[dict] = []
        self.generate_calls: List[dict] = []
        self.count_error: Optional[BaseException] = None
        self.generate_error: Optional[BaseException] = None
        self.on_generate: Optional[Callable[[], object]] = None  # observer / bloquer pendant l'émission
        self.gate: Optional[asyncio.Event] = None

    async def count_tokens(self, request) -> dict:
        self.count_calls.append({"url_model": request.model, "body": copy.deepcopy(request.count_tokens_body())})
        if self.count_error is not None:
            raise self.count_error
        return {"totalTokens": self.count}

    async def generate(self, request) -> dict:
        self.generate_calls.append({"url_model": request.model, "body": copy.deepcopy(request.generate_body())})
        if self.on_generate is not None:
            self.on_generate()
        if self.gate is not None:
            await self.gate.wait()
        if self.generate_error is not None:
            raise self.generate_error
        return copy.deepcopy(self.response)


def http_error(status: int) -> UpstreamHTTPError:
    return UpstreamHTTPError(status, "fixture")


def transport_error(*, before_send: bool) -> UpstreamTransportError:
    return UpstreamTransportError("fixture", before_send=before_send)


def bad_body() -> UpstreamBadBody:
    return UpstreamBadBody("fixture")


def chat_request(content: str = "bonjour", **overrides) -> dict:
    body = {"model": "gemma-4-31b-it", "messages": [{"role": "user", "content": content}], "max_tokens": 64}
    body.update(overrides)
    return body
