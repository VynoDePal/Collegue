"""Normalisation d'une requête Chat Completions vers l'objet natif Google ``generateContentRequest`` — et retour.

Principes (contrat W5) :

* le client (SDK OpenAI / LiteLLM) parle Chat Completions ; le fournisseur reçoit du Google NATIF (``contents``,
  ``systemInstruction``, ``tools.functionDeclarations``, ``toolConfig``, ``generationConfig``) ;
* **aucun** transfert libre : liste blanche de champs, de rôles de messages et de types de contenu ; tout champ inconnu,
  tout média / URL, tout streaming est refusé avec le NOM du champ fautif ;
* JSON strict : doublons de clés, ``NaN``/``Infinity``, profondeur et taille au-delà des plafonds sont refusés ;
* UNE seule limite de sortie : ``max_tokens`` / ``max_completion_tokens`` contradictoires sont refusés, une limite au-delà
  du plafond du serveur est REFUSÉE (jamais réduite en silence), une limite absente prend le défaut du serveur ;
* ``countTokens`` et ``generateContent`` portent le MÊME objet normalisé (:class:`NormalizedRequest`) ;
* l'usage inclut le raisonnement (``thoughtsTokenCount``) sans double addition : ``candidatesTokenCount`` l'exclut, et la
  somme des composantes DOIT égaler ``totalTokenCount`` sinon l'usage est déclaré incohérent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from collegue.broker.errors import (
    BrokerBoundViolation,
    BrokerForbidden,
    BrokerRequestRefused,
    BrokerUnsupported,
)
from collegue.broker.policy import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_JSON_DEPTH,
    MAX_OUTPUT_TOKENS_CEILING,
    MAX_REQUEST_BYTES,
    canonical_model,
)

_ALLOWED_TOP_LEVEL = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "stop",
        "n",
        "stream",
        "seed",
        "parallel_tool_calls",
    }
)
_MESSAGE_KEYS = {
    "system": {"role", "content", "name"},
    "developer": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "name", "tool_calls", "refusal"},
    "tool": {"role", "content", "tool_call_id", "name"},
}
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")
_MAX_TOOLS = 64
_MAX_TOOL_SCHEMA_BYTES = 32 * 1024
_MAX_MESSAGES = 512
_MAX_STOP = 4


# ── JSON strict ──────────────────────────────────────────────────────────────────────────────────────────────


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise BrokerRequestRefused(f"clé JSON dupliquée : {key!r}", code="duplicate_json_key")
        seen[key] = value
    return seen


def _reject_constant(name: str):
    raise BrokerRequestRefused(f"constante JSON non finie refusée : {name}", code="invalid_json")


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def parse_json_strict(raw: bytes, *, max_bytes: int = MAX_REQUEST_BYTES) -> Any:
    """Décode un corps JSON en refusant taille excessive, UTF-8 invalide, doublons, ``NaN``/``Infinity`` et profondeur."""
    if len(raw) > max_bytes:
        raise BrokerRequestRefused(
            f"corps de requête trop gros ({len(raw)} > {max_bytes} octets)", code="payload_too_large", status=413
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BrokerRequestRefused("corps de requête non UTF-8", code="invalid_json") from exc
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise BrokerRequestRefused(f"JSON invalide : {exc.msg}", code="invalid_json") from exc
    except RecursionError as exc:
        raise BrokerRequestRefused("JSON trop profond", code="invalid_json") from exc
    if _depth(value) > MAX_JSON_DEPTH:
        raise BrokerRequestRefused(f"JSON trop profond (> {MAX_JSON_DEPTH})", code="invalid_json")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ── requête normalisée ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NormalizedRequest:
    """L'objet UNIQUE envoyé à ``countTokens`` (enveloppé) et à ``generateContent`` (tel quel)."""

    model: str  # nom nu canonique
    body: dict  # generateContentRequest complet (inclut ``model: models/<nom>``)
    output_cap: int
    sha256: str
    tool_names: Tuple[str, ...] = ()
    requested_model: str = ""
    extra: dict = field(default_factory=dict)

    def count_tokens_body(self) -> dict:
        """Corps de ``models/{model}:countTokens`` : le ``generateContentRequest`` COMPLET (instructions et outils compris)."""
        return {"generateContentRequest": self.body}

    def generate_body(self) -> dict:
        """Corps de ``models/{model}:generateContent`` : exactement le même objet."""
        return self.body


def _int_field(name: str, value: Any, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BrokerRequestRefused(f"{name} invalide : entier ≥ {minimum} requis", code="invalid_parameter")
    return value


def _number_field(name: str, value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BrokerRequestRefused(f"{name} invalide : nombre fini requis", code="invalid_parameter")
    if not low <= value <= high:
        raise BrokerRequestRefused(f"{name} hors de [{low}, {high}]", code="invalid_parameter")
    return float(value)


def _text_of(content: Any, where: str) -> str:
    """Texte d'un ``content`` (chaîne ou liste de parties ``text``) ; tout autre type (image, audio, fichier, URL) est refusé."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise BrokerRequestRefused(f"{where}.content : chaîne ou liste de parties attendue", code="invalid_message")
    pieces: List[str] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise BrokerRequestRefused(f"{where}.content[{index}] : objet attendu", code="invalid_message")
        kind = part.get("type")
        if kind != "text":
            raise BrokerUnsupported(
                f"{where}.content[{index}] : partie {kind!r} refusée (médias, fichiers et URL ne sont pas pris en charge)"
            )
        extra = set(part) - {"type", "text", "cache_control"}
        if extra:
            raise BrokerRequestRefused(
                f"{where}.content[{index}] : champ(s) inconnu(s) {sorted(extra)}", code="invalid_message"
            )
        cache = part.get("cache_control")
        if cache is not None and cache != {"type": "ephemeral"}:
            raise BrokerRequestRefused(
                f"{where}.content[{index}].cache_control non pris en charge", code="invalid_message"
            )
        text = part.get("text")
        if not isinstance(text, str):
            raise BrokerRequestRefused(f"{where}.content[{index}].text : chaîne attendue", code="invalid_message")
        pieces.append(text)
    return "".join(pieces)


def _json_object(text: str, where: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise BrokerRequestRefused(f"{where} : JSON invalide ({exc.msg})", code="invalid_message") from exc
    if not isinstance(value, dict):
        raise BrokerRequestRefused(f"{where} : un objet JSON est requis", code="invalid_message")
    return value


def _convert_messages(messages: Any) -> Tuple[List[dict], Optional[dict]]:
    if not isinstance(messages, list) or not messages:
        raise BrokerRequestRefused("messages : liste non vide requise", code="invalid_message")
    if len(messages) > _MAX_MESSAGES:
        raise BrokerRequestRefused(f"messages : au plus {_MAX_MESSAGES}", code="invalid_message")
    system: List[str] = []
    contents: List[dict] = []
    call_names: Dict[str, str] = {}

    def push(role: str, parts: List[dict]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)  # tours consécutifs de même rôle (réponses d'outils parallèles)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        if not isinstance(message, dict):
            raise BrokerRequestRefused(f"{where} : objet attendu", code="invalid_message")
        role = message.get("role")
        if role not in _MESSAGE_KEYS:
            raise BrokerRequestRefused(f"{where}.role {role!r} non pris en charge", code="invalid_message")
        unknown = set(message) - _MESSAGE_KEYS[role]
        if unknown:
            raise BrokerRequestRefused(f"{where} : champ(s) inconnu(s) {sorted(unknown)}", code="invalid_message")
        if role in ("system", "developer"):
            system.append(_text_of(message.get("content"), where))
        elif role == "user":
            push("user", [{"text": _text_of(message.get("content"), where)}])
        elif role == "assistant":
            parts: List[dict] = []
            text = _text_of(message.get("content"), where)
            if text:
                parts.append({"text": text})
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                raise BrokerRequestRefused(f"{where}.tool_calls : liste attendue", code="invalid_message")
            for position, call in enumerate(calls):
                spot = f"{where}.tool_calls[{position}]"
                if (
                    not isinstance(call, dict)
                    or call.get("type") != "function"
                    or not isinstance(call.get("function"), dict)
                ):
                    raise BrokerRequestRefused(f"{spot} : appel de fonction attendu", code="invalid_message")
                if set(call) - {"id", "type", "function"} or set(call["function"]) - {"name", "arguments"}:
                    raise BrokerRequestRefused(f"{spot} : champ(s) inconnu(s)", code="invalid_message")
                name, call_id = call["function"].get("name"), call.get("id")
                if (
                    not isinstance(name, str)
                    or not _TOOL_NAME.match(name)
                    or not isinstance(call_id, str)
                    or not call_id
                ):
                    raise BrokerRequestRefused(f"{spot} : id et nom de fonction valides requis", code="invalid_message")
                arguments = call["function"].get("arguments", "{}")
                args = _json_object(arguments if isinstance(arguments, str) else "null", f"{spot}.arguments")
                call_names[call_id] = name
                parts.append({"functionCall": {"name": name, "args": args}})
            if not parts:
                raise BrokerRequestRefused(f"{where} : message assistant vide", code="invalid_message")
            push("model", parts)
        else:  # tool
            call_id = message.get("tool_call_id")
            name = call_names.get(call_id) if isinstance(call_id, str) else None
            if name is None:
                raise BrokerRequestRefused(
                    f"{where}.tool_call_id sans appel d'outil correspondant dans les messages précédents",
                    code="invalid_message",
                )
            text = _text_of(message.get("content"), where)
            try:
                response = _json_object(text, where)
            except BrokerRequestRefused:
                response = {"result": text}
            push("user", [{"functionResponse": {"name": name, "response": response}}])
    if not contents:
        raise BrokerRequestRefused("messages : au moins un message non système requis", code="invalid_message")
    instruction = {"parts": [{"text": "\n\n".join(system)}]} if any(system) else None
    return contents, instruction


def _convert_tools(tools: Any) -> Tuple[Optional[list], Tuple[str, ...]]:
    if tools is None:
        return None, ()
    if not isinstance(tools, list) or not tools or len(tools) > _MAX_TOOLS:
        raise BrokerRequestRefused(f"tools : liste de 1 à {_MAX_TOOLS} outils attendue", code="invalid_tools")
    declarations: List[dict] = []
    names: List[str] = []
    for index, tool in enumerate(tools):
        where = f"tools[{index}]"
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise BrokerRequestRefused(f"{where} : seul le type 'function' est pris en charge", code="invalid_tools")
        if set(tool) - {"type", "function"}:
            raise BrokerRequestRefused(
                f"{where} : champ(s) inconnu(s) {sorted(set(tool) - {'type', 'function'})}", code="invalid_tools"
            )
        function = tool["function"]
        unknown = set(function) - {"name", "description", "parameters", "strict"}
        if unknown:
            raise BrokerRequestRefused(
                f"{where}.function : champ(s) inconnu(s) {sorted(unknown)}", code="invalid_tools"
            )
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.match(name) or name in names:
            raise BrokerRequestRefused(f"{where}.function.name invalide ou dupliqué", code="invalid_tools")
        declaration: dict = {"name": name}
        description = function.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise BrokerRequestRefused(f"{where}.function.description : chaîne attendue", code="invalid_tools")
            declaration["description"] = description
        parameters = function.get("parameters")
        if parameters is not None:
            if not isinstance(parameters, dict) or len(canonical_json(parameters)) > _MAX_TOOL_SCHEMA_BYTES:
                raise BrokerRequestRefused(
                    f"{where}.function.parameters : objet JSON Schema ≤ 32 Kio attendu", code="invalid_tools"
                )
            declaration["parametersJsonSchema"] = parameters
        declarations.append(declaration)
        names.append(name)
    return [{"functionDeclarations": declarations}], tuple(names)


def _convert_tool_choice(choice: Any, names: Tuple[str, ...]) -> Optional[dict]:
    if choice is None:
        return None
    if choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if not names:
        raise BrokerRequestRefused("tool_choice sans tools", code="invalid_tools")
    if choice == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice == "required":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if (
        isinstance(choice, dict)
        and choice.get("type") == "function"
        and isinstance(choice.get("function"), dict)
        and set(choice) == {"type", "function"}
        and set(choice["function"]) == {"name"}
        and choice["function"]["name"] in names
    ):
        return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [choice["function"]["name"]]}}
    raise BrokerRequestRefused("tool_choice invalide ou nommant un outil inconnu", code="invalid_tools")


def normalize_chat_request(
    payload: Any,
    *,
    allowed_models: Tuple[str, ...],
    default_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_output_tokens: int = MAX_OUTPUT_TOKENS_CEILING,
) -> NormalizedRequest:
    """Valide ``payload`` (déjà décodé par :func:`parse_json_strict`) et construit l'objet Google normalisé."""
    if not isinstance(payload, dict):
        raise BrokerRequestRefused("le corps doit être un objet JSON", code="invalid_json")
    unknown = sorted(set(payload) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise BrokerRequestRefused(
            f"champ(s) non pris en charge : {unknown} (aucun paramètre n'est transféré librement)",
            code="unsupported_field",
        )
    requested = payload.get("model")
    try:
        model = canonical_model(requested)
    except ValueError as exc:
        raise BrokerForbidden(str(exc), code="model_not_allowed") from exc
    if model not in allowed_models:
        raise BrokerForbidden(f"modèle {model!r} non autorisé pour cette session", code="model_not_allowed")
    if payload.get("stream") not in (None, False):
        raise BrokerUnsupported("stream=true refusé : le streaming n'est pas pris en charge")
    if payload.get("n") not in (None, 1) or isinstance(payload.get("n"), bool):
        raise BrokerRequestRefused("n doit valoir 1 (un seul candidat)", code="invalid_parameter")
    if payload.get("parallel_tool_calls") not in (None, True):
        raise BrokerRequestRefused("parallel_tool_calls=false ne peut pas être garanti", code="invalid_parameter")

    contents, instruction = _convert_messages(payload.get("messages"))
    tools, tool_names = _convert_tools(payload.get("tools"))
    tool_config = _convert_tool_choice(payload.get("tool_choice"), tool_names)

    caps = {
        key: _int_field(key, payload[key])
        for key in ("max_tokens", "max_completion_tokens")
        if payload.get(key) is not None
    }
    if len(set(caps.values())) > 1:
        raise BrokerRequestRefused(
            "max_tokens et max_completion_tokens contradictoires : une seule limite de sortie est permise",
            code="contradictory_output_limit",
        )
    cap = next(iter(caps.values())) if caps else default_output_tokens
    if cap > max_output_tokens:
        # Jamais réduite en silence : la violation est signalée.
        raise BrokerRequestRefused(
            f"limite de sortie {cap} > plafond du serveur {max_output_tokens} : refusée (aucun écrêtage)",
            code="output_limit_exceeds_ceiling",
        )

    generation: dict = {"candidateCount": 1, "maxOutputTokens": cap}
    if payload.get("temperature") is not None:
        generation["temperature"] = _number_field("temperature", payload["temperature"], 0.0, 2.0)
    if payload.get("top_p") is not None:
        generation["topP"] = _number_field("top_p", payload["top_p"], 0.0, 1.0)
    if payload.get("seed") is not None:
        generation["seed"] = _int_field("seed", payload["seed"], minimum=0)
    stop = payload.get("stop")
    if stop is not None:
        sequences = [stop] if isinstance(stop, str) else stop
        if (
            not isinstance(sequences, list)
            or not sequences
            or len(sequences) > _MAX_STOP
            or not all(isinstance(item, str) and item for item in sequences)
        ):
            raise BrokerRequestRefused(f"stop : 1 à {_MAX_STOP} chaînes non vides attendues", code="invalid_parameter")
        generation["stopSequences"] = list(sequences)
    fmt = payload.get("response_format")
    if fmt is not None:
        if not isinstance(fmt, dict):
            raise BrokerRequestRefused("response_format : objet attendu", code="invalid_parameter")
        kind = fmt.get("type")
        if kind == "text" and set(fmt) == {"type"}:
            pass
        elif kind == "json_object" and set(fmt) == {"type"}:
            generation["responseMimeType"] = "application/json"
        elif kind == "json_schema" and set(fmt) == {"type", "json_schema"} and isinstance(fmt["json_schema"], dict):
            schema = fmt["json_schema"].get("schema")
            if set(fmt["json_schema"]) - {"name", "schema", "strict", "description"} or not isinstance(schema, dict):
                raise BrokerRequestRefused(
                    "response_format.json_schema : {name, schema} attendu", code="invalid_parameter"
                )
            if len(canonical_json(schema)) > _MAX_TOOL_SCHEMA_BYTES:
                raise BrokerRequestRefused("response_format.json_schema.schema trop gros", code="invalid_parameter")
            generation["responseMimeType"] = "application/json"
            generation["responseJsonSchema"] = schema
        else:
            raise BrokerRequestRefused("response_format non pris en charge", code="invalid_parameter")

    body: dict = {"model": f"models/{model}", "contents": contents, "generationConfig": generation}
    if instruction is not None:
        body["systemInstruction"] = instruction
    if tools is not None:
        body["tools"] = tools
    if tool_config is not None:
        body["toolConfig"] = tool_config
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return NormalizedRequest(
        model=model, body=body, output_cap=cap, sha256=digest, tool_names=tool_names, requested_model=str(requested)
    )


# ── usage ────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Usage:
    """Usage Google validé. ``consumed_tokens`` = entrée + entrée d'outils + candidats + raisonnement (jamais deux fois)."""

    prompt: int
    tool_use_prompt: int
    candidates: int
    thoughts: int
    total: int

    @property
    def consumed_tokens(self) -> int:
        return self.prompt + self.tool_use_prompt + self.candidates + self.thoughts

    @property
    def output_tokens(self) -> int:
        return self.candidates + self.thoughts


def _usage_int(metadata: dict, key: str, *, required: bool) -> int:
    value = metadata.get(key)
    if value is None:
        if required:
            raise BrokerBoundViolation(f"usage absent ou incomplet : {key} manquant", code="usage_missing")
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BrokerBoundViolation(f"usage invalide : {key}={value!r}", code="usage_invalid")
    return value


def parse_usage(response: Any) -> Usage:
    """Extrait et VÉRIFIE ``usageMetadata`` : absent / invalide / incohérent ⇒ :class:`BrokerBoundViolation`, jamais zéro."""
    metadata = response.get("usageMetadata") if isinstance(response, dict) else None
    if not isinstance(metadata, dict):
        raise BrokerBoundViolation("usage absent de la réponse du fournisseur", code="usage_missing")
    usage = Usage(
        prompt=_usage_int(metadata, "promptTokenCount", required=True),
        tool_use_prompt=_usage_int(metadata, "toolUsePromptTokenCount", required=False),
        candidates=_usage_int(metadata, "candidatesTokenCount", required=False),
        thoughts=_usage_int(metadata, "thoughtsTokenCount", required=False),
        total=_usage_int(metadata, "totalTokenCount", required=True),
    )
    if usage.consumed_tokens != usage.total:
        raise BrokerBoundViolation(
            f"usage incohérent : entrée {usage.prompt} + outils {usage.tool_use_prompt} + candidats {usage.candidates} + "
            f"raisonnement {usage.thoughts} ≠ total {usage.total}",
            code="usage_inconsistent",
        )
    return usage


# ── réponse ──────────────────────────────────────────────────────────────────────────────────────────────────

_FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
}


def translate_response(response: Any, request: NormalizedRequest) -> Tuple[dict, Usage]:
    """Réponse Google → ``(chat.completion, usage)``. Une forme inattendue est une violation (la génération a eu lieu)."""
    usage = parse_usage(response)
    candidates = response.get("candidates") if isinstance(response, dict) else None
    feedback = response.get("promptFeedback") if isinstance(response, dict) else None
    if candidates in (None, []):
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            message: dict = {"role": "assistant", "content": None}
            finish = "content_filter"
        else:
            raise BrokerBoundViolation("réponse sans candidat ni blocage déclaré", code="response_invalid")
    else:
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise BrokerBoundViolation(
                "candidateCount=1 demandé mais la réponse en porte un autre nombre", code="response_invalid"
            )
        candidate = candidates[0]
        parts = ((candidate.get("content") or {}).get("parts")) or []
        if not isinstance(parts, list):
            raise BrokerBoundViolation("parts de réponse invalides", code="response_invalid")
        texts: List[str] = []
        calls: List[dict] = []
        for part in parts:
            if not isinstance(part, dict):
                raise BrokerBoundViolation("part de réponse invalide", code="response_invalid")
            if part.get("thought") is True:
                continue  # le raisonnement est compté dans l'usage mais jamais renvoyé comme contenu
            if "functionCall" in part:
                call = part["functionCall"]
                name = call.get("name") if isinstance(call, dict) else None
                args = call.get("args", {}) if isinstance(call, dict) else None
                if not isinstance(name, str) or not isinstance(args, dict):
                    raise BrokerBoundViolation("functionCall invalide", code="response_invalid")
                arguments = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
                call_id = (
                    "call_"
                    + hashlib.sha256(f"{request.sha256}:{len(calls)}:{name}:{arguments}".encode()).hexdigest()[:24]
                )
                calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
            elif isinstance(part.get("text"), str):
                texts.append(part["text"])
        message = {"role": "assistant", "content": "".join(texts) if texts else None}
        if calls:
            message["tool_calls"] = calls
        reason = str(candidate.get("finishReason") or "")
        finish = "tool_calls" if calls else _FINISH.get(reason)
        if finish is None:
            raise BrokerBoundViolation(f"finishReason inconnu : {reason!r}", code="response_invalid")
    completion = {
        "id": "chatcmpl-" + request.sha256[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage.prompt + usage.tool_use_prompt,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.total,
            "completion_tokens_details": {"reasoning_tokens": usage.thoughts},
        },
    }
    return completion, usage


def parse_count_tokens(response: Any) -> int:
    """``totalTokens`` de ``countTokens`` ; absent ou invalide ⇒ refus explicite (aucune estimation de remplacement)."""
    total = response.get("totalTokens") if isinstance(response, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise BrokerUnsupported(
            "countTokens n'a pas renvoyé totalTokens : réservation impossible, aucune estimation de remplacement"
        )
    return total
