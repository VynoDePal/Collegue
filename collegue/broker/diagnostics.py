"""Diagnostic FERMÉ des réponses HTTP non-200 de Google (A37).

Objectif : pouvoir CLASSER une erreur amont (catégorie RPC, nature du corps) sans jamais conserver ce que le fournisseur a écrit. Rien de ce
module n'est jamais recopié depuis la réponse : chaque valeur est choisie dans une liste fermée écrite ici, ou calculée (statut HTTP entier,
booléen). Pas de message libre, pas de corps, pas d'URL (donc pas de ``Location``), pas de valeur d'en-tête, pas d'identifiant, pas de hash.

Ce que ces catégories ne font PAS : elles ne prouvent ni l'origine (Google ou intermédiaire) ni la cause interne d'un échec — un corps JSON ou
HTML ne dit pas qui l'a émis. Elles ne décident JAMAIS de la comptabilité : seul le statut HTTP (politique de ``service.py``) tranche.

Tout est fail-safe : un corps illisible, des doublons, des types inattendus, une profondeur excessive ou des en-têtes qui lèvent produisent
des valeurs de repli ; ``http_diagnostic`` ne lève jamais.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from collegue.broker.policy import MAX_RESPONSE_BYTES
from collegue.broker.translate import parse_json_strict

OPERATIONS = ("countTokens", "generateContent")
BODY_KINDS = ("json", "html", "other", "empty", "invalid_json")
#: Statuts Google RPC reconnus (``google.rpc.Code``). Toute autre valeur est « other » ; l'absence est « absent ».
RPC_STATUSES = frozenset(
    {
        "OK",
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
    }
)
RPC_ABSENT = "absent"
RPC_OTHER = "other"
#: En-têtes dont la SEULE présence (valeur non vide) est notée. La valeur n'est jamais lue au-delà de ce test, jamais conservée.
REQUEST_ID_HEADERS = ("x-goog-request-id", "x-request-id", "request-id", "x-cloud-trace-context")

_KEYS = ("operation", "http_status", "body_kind", "rpc_status", "request_id_present")
_MISSING = object()


def _rpc_status(value: Any) -> str:
    if not isinstance(value, dict):
        return RPC_ABSENT
    error = value.get("error")
    if not isinstance(error, dict) or "status" not in error:
        return RPC_ABSENT
    status = error["status"]
    return status if isinstance(status, str) and status in RPC_STATUSES else RPC_OTHER


def _classify_body(raw: bytes, content_type: str) -> tuple:
    """``(body_kind, valeur JSON ou _MISSING)``. Aucune déduction d'origine : une simple nature de contenu."""
    if not raw.strip():
        return "empty", _MISSING
    try:
        return "json", parse_json_strict(
            raw, max_bytes=MAX_RESPONSE_BYTES
        )  # refuse doublons, profondeur, constantes non finies
    except Exception:  # noqa: BLE001 - tout échec d'analyse est « invalid_json » (RecursionError comprise), jamais une exception
        pass
    first = raw.lstrip()[:1]
    if "json" in content_type or first in (b"{", b"["):
        return "invalid_json", _MISSING
    head = raw.lstrip()[:15].lower()
    if "html" in content_type or head.startswith((b"<!doctype html", b"<html")):
        return "html", _MISSING
    return "other", _MISSING


def _header(headers: Any, name: str) -> str:
    try:
        value = headers.get(name)
    except Exception:  # noqa: BLE001 - des en-têtes illisibles ne cassent rien
        return ""
    return value if isinstance(value, str) else ""


def http_diagnostic(operation: str, http_status: int, response: Any, raw: bytes) -> dict:
    """Diagnostic fermé d'une réponse HTTP non-200 observée. ``response`` expose ``.headers`` ; ne lève jamais."""
    try:
        headers = getattr(response, "headers", None)
        content_type = _header(headers, "content-type").lower()
        body_kind, value = _classify_body(bytes(raw or b""), content_type)
        rpc = _rpc_status(value) if body_kind == "json" else RPC_ABSENT
        request_id = any(_header(headers, name).strip() for name in REQUEST_ID_HEADERS)
    except Exception:  # noqa: BLE001
        body_kind, rpc, request_id = "other", RPC_ABSENT, False
    return sanitize_diagnostic(
        {
            "operation": operation,
            "http_status": http_status,
            "body_kind": body_kind,
            "rpc_status": rpc,
            "request_id_present": request_id,
        }
    )


def sanitize_diagnostic(value: Any) -> dict:
    """Ramène ``value`` aux seules valeurs fermées (``{}`` si elle n'est pas fiable). Les clés inconnues sont ignorées."""
    if not isinstance(value, Mapping) or not value:
        return {}
    operation, status = value.get("operation"), value.get("http_status")
    if not (isinstance(operation, str) and operation in OPERATIONS):
        return {}
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return {}
    body_kind = value.get("body_kind")
    rpc = value.get("rpc_status")
    return {
        "operation": operation,
        "http_status": status,
        "body_kind": body_kind if isinstance(body_kind, str) and body_kind in BODY_KINDS else "other",
        "rpc_status": rpc if isinstance(rpc, str) and (rpc in RPC_STATUSES or rpc == RPC_ABSENT) else RPC_OTHER,
        "request_id_present": value.get("request_id_present") is True,
    }


def diagnostic_suffix(exc: BaseException) -> str:
    """Suffixe court (ASCII, valeurs fermées) à ajouter aux messages contrôlés et à ``error_detail``. ``""`` sans diagnostic."""
    data = sanitize_diagnostic(getattr(exc, "diagnostic", None))
    if not data:
        return ""
    present = "present" if data["request_id_present"] else "absent"
    return (
        f" [diag http={data['http_status']} op={data['operation']} rpc={data['rpc_status']}"
        f" body={data['body_kind']} request_id={present}]"
    )


#: Longueur maximale examinée d'un ``error_detail`` historique : le suffixe produit par :func:`diagnostic_suffix` fait moins de 100 caractères.
_DETAIL_TAIL = 160
_HISTORIC_SUFFIX = re.compile(
    r" \[diag http=([1-5][0-9]{2}) op=("
    + "|".join(OPERATIONS)
    + r") rpc=("
    + "|".join(sorted(RPC_STATUSES | {RPC_ABSENT, RPC_OTHER}))
    + r")"
    r" body=(" + "|".join(BODY_KINDS) + r") request_id=(present|absent)\]\Z"
)


def suffix_from_durable_detail(detail: Optional[str]) -> str:
    """Suffixe SÛR reconstruit depuis un ``error_detail`` DURABLE (reprise), ou ``""``.

    Le détail historique n'est jamais recopié : seules les CINQ valeurs fermées du suffixe de fin de chaîne (``diagnostic_suffix``) sont extraites, validées contre
    les listes fermées, puis le suffixe est RECONSTRUIT par :func:`diagnostic_suffix`. Suffixe absent, incomplet, mal formé, hors liste ou non terminal
    (texte libre après lui) ⇒ ``""`` : l'appelant garde alors son message historique. Ne lève jamais.
    """
    try:
        if not isinstance(detail, str):
            return ""
        match = _HISTORIC_SUFFIX.search(detail[-_DETAIL_TAIL:])
        if match is None:
            return ""
        status, operation, rpc, body_kind, request_id = match.groups()
        return diagnostic_suffix(
            _Carrier(
                {
                    "operation": operation,
                    "http_status": int(status),
                    "body_kind": body_kind,
                    "rpc_status": rpc,
                    "request_id_present": request_id == "present",
                }
            )
        )
    except Exception:  # noqa: BLE001
        return ""


class _Carrier:
    """Porte un dictionnaire de diagnostic sous le nom d'attribut attendu par :func:`diagnostic_suffix`."""

    def __init__(self, diagnostic: dict):
        self.diagnostic = diagnostic
