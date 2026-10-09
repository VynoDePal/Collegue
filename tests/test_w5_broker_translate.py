"""Traduction Chat Completions → ``generateContentRequest`` natif Google, et réponse / usage au retour."""

from __future__ import annotations

import json

import pytest

from collegue.broker.errors import BrokerBoundViolation, BrokerForbidden, BrokerRequestRefused, BrokerUnsupported
from collegue.broker.translate import (
    normalize_chat_request,
    parse_count_tokens,
    parse_json_strict,
    parse_usage,
    translate_response,
)

BOTH = ("gemma-4-31b-it", "gemma-4-26b-a4b-it")


def norm(**payload):
    base = {"model": "gemma-4-31b-it", "messages": [{"role": "user", "content": "x"}], "max_tokens": 32}
    base.update(payload)
    return normalize_chat_request(base, allowed_models=BOTH)


def test_system_and_developer_messages_become_the_system_instruction_and_roles_are_mapped():
    nr = norm(
        messages=[
            {"role": "system", "content": "règle 1"},
            {"role": "developer", "content": [{"type": "text", "text": "règle 2"}]},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "r"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}},
                ],
            },
        ]
    )
    assert nr.body["systemInstruction"] == {"parts": [{"text": "règle 1\n\nrègle 2"}]}
    assert [(c["role"], c["parts"]) for c in nr.body["contents"]] == [
        ("user", [{"text": "q"}]),
        ("model", [{"text": "r"}]),
        ("user", [{"text": "ab"}]),
    ]


def test_tool_declarations_choices_calls_and_results_are_translated_natively():
    schema = {
        "type": "object",
        "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"],
        "additionalProperties": False,
    }
    tools = [
        {"type": "function", "function": {"name": "run", "description": "exécute", "parameters": schema}},
        {"type": "function", "function": {"name": "noop"}},
    ]
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "run", "arguments": '{"cmd":"ls"}'}},
                {"id": "c2", "type": "function", "function": {"name": "noop", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"out":"a.txt"}'},
        {"role": "tool", "tool_call_id": "c2", "content": "texte brut"},
    ]
    nr = norm(messages=messages, tools=tools, tool_choice={"type": "function", "function": {"name": "run"}})
    declarations = nr.body["tools"][0]["functionDeclarations"]
    assert declarations[0] == {
        "name": "run",
        "description": "exécute",
        "parametersJsonSchema": schema,
    } and declarations[1] == {"name": "noop"}
    assert nr.body["toolConfig"] == {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["run"]}}
    assert nr.body["contents"][1] == {
        "role": "model",
        "parts": [
            {"functionCall": {"name": "run", "args": {"cmd": "ls"}}},
            {"functionCall": {"name": "noop", "args": {}}},
        ],
    }
    assert nr.body["contents"][2] == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "run", "response": {"out": "a.txt"}}},
            {"functionResponse": {"name": "noop", "response": {"result": "texte brut"}}},
        ],
    }  # réponses parallèles regroupées
    assert nr.tool_names == ("run", "noop")


@pytest.mark.parametrize(
    "choice, expected",
    [("auto", {"mode": "AUTO"}), ("none", {"mode": "NONE"}), ("required", {"mode": "ANY"})],
)
def test_tool_choice_modes(choice, expected):
    tools = [{"type": "function", "function": {"name": "t"}}]
    assert norm(tools=tools, tool_choice=choice).body["toolConfig"] == {"functionCallingConfig": expected}


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_choice": "auto"},  # sans tools
        {
            "tools": [],
        },
        {"tools": [{"type": "function", "function": {"name": "a"}}, {"type": "function", "function": {"name": "a"}}]},
        {"tools": [{"type": "retrieval"}]},
        {"tools": [{"type": "function", "function": {"name": "bad name"}}]},
        {"tools": [{"type": "function", "function": {"name": "t", "extra": 1}}]},
        {"tools": [{"type": "function", "function": {"name": "t", "parameters": {"x": "y" * 40000}}}]},
        {
            "tools": [{"type": "function", "function": {"name": "t"}}],
            "tool_choice": {"type": "function", "function": {"name": "autre"}},
        },
        {"tools": [{"type": "function", "function": {"name": "t"}}], "tool_choice": "bizarre"},
    ],
)
def test_invalid_tools_are_refused_with_a_stable_code(extra):
    with pytest.raises(BrokerRequestRefused) as caught:
        norm(**extra)
    assert caught.value.code == "invalid_tools"


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "inconnu", "content": "x"}],
        [{"role": "user", "content": "x", "extra": 1}],
        [{"role": "assistant", "content": ""}],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "t", "arguments": "[1]"}}],
            }
        ],
        [{"role": "user", "content": [{"type": "text", "text": 5}]}],
        [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}],
        [{"role": "user", "content": 5}],
    ],
)
def test_invalid_or_unsupported_messages_are_refused(messages):
    with pytest.raises((BrokerRequestRefused, BrokerUnsupported)):
        norm(messages=messages)


def test_response_format_variants():
    assert "responseMimeType" not in norm(response_format={"type": "text"}).body["generationConfig"]
    assert (
        norm(response_format={"type": "json_object"}).body["generationConfig"]["responseMimeType"] == "application/json"
    )
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    config = norm(response_format={"type": "json_schema", "json_schema": {"name": "r", "schema": schema}}).body[
        "generationConfig"
    ]
    assert config["responseMimeType"] == "application/json" and config["responseJsonSchema"] == schema
    for bad in (
        {"type": "xml"},
        {"type": "json_object", "x": 1},
        {"type": "json_schema", "json_schema": {"name": "r"}},
        "json",
    ):
        with pytest.raises(BrokerRequestRefused):
            norm(response_format=bad)


def test_sampling_parameters_are_validated_and_translated_never_forwarded_raw():
    config = norm(temperature=0.2, top_p=0.9, seed=7, stop=["fin", "stop"]).body["generationConfig"]
    assert config == {
        "candidateCount": 1,
        "maxOutputTokens": 32,
        "temperature": 0.2,
        "topP": 0.9,
        "seed": 7,
        "stopSequences": ["fin", "stop"],
    }
    for bad in (
        {"temperature": 3},
        {"temperature": float("nan")},
        {"temperature": True},
        {"top_p": -0.1},
        {"seed": -1},
        {"stop": []},
        {"stop": ["a", "b", "c", "d", "e"]},
        {"stop": [""]},
    ):
        with pytest.raises(BrokerRequestRefused):
            norm(**bad)


def test_the_model_must_be_one_of_the_allowed_identities_whatever_its_spelling():
    assert norm(model="models/gemma-4-31b-it").model == "gemma-4-31b-it"
    assert norm(model="gemini/gemma-4-26b-a4b-it").model == "gemma-4-26b-a4b-it"
    for bad in ("gemma-4-31b", "gemma-4-31b-it ", "GEMMA-4-31B-IT", "gemma-4-31b-it/../x", "", None, 5):
        with pytest.raises(BrokerForbidden):
            norm(model=bad)
    only_primary = (
        normalize_chat_request(
            {"model": "gemma-4-26b-a4b-it", "messages": [{"role": "user", "content": "x"}]},
            allowed_models=("gemma-4-31b-it",),
        )
        if False
        else None
    )
    with pytest.raises(BrokerForbidden):
        normalize_chat_request(
            {"model": "gemma-4-26b-a4b-it", "messages": [{"role": "user", "content": "x"}]},
            allowed_models=("gemma-4-31b-it",),
        )
    assert only_primary is None


def test_the_normalized_digest_is_deterministic_and_content_sensitive():
    assert norm().sha256 == norm().sha256 and norm().sha256 != norm(temperature=0.1).sha256
    assert (
        norm(messages=[{"role": "user", "content": "a"}]).sha256
        != norm(messages=[{"role": "user", "content": "b"}]).sha256
    )


def test_json_strictness_depth_and_size():
    assert parse_json_strict(b'{"a":[1,2]}') == {"a": [1, 2]}
    deep = b"[" * 40 + b"]" * 40
    for raw in (b'{"a":1,"a":1}', b"NaN", b'{"x":Infinity}', b"\xff\xfe", deep, b"{" + b" " * 300000 + b"}"):
        with pytest.raises(BrokerRequestRefused):
            parse_json_strict(raw)


def google(parts, *, finish="STOP", usage=None):
    body = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}
    body["usageMetadata"] = (
        usage if usage is not None else {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}
    )
    return body


def test_text_function_call_and_thought_responses():
    nr = norm()
    completion, usage = translate_response(
        google([{"text": "a"}, {"text": "pensée", "thought": True}, {"text": "b"}]), nr
    )
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": "ab"} and usage.consumed_tokens == 10
    assert completion["choices"][0]["finish_reason"] == "stop" and completion["object"] == "chat.completion"
    call, _ = translate_response(google([{"functionCall": {"name": "run", "args": {"cmd": "ls"}}}]), nr)
    message = call["choices"][0]["message"]
    assert call["choices"][0]["finish_reason"] == "tool_calls" and message["content"] is None
    assert message["tool_calls"][0]["function"] == {"name": "run", "arguments": '{"cmd":"ls"}'} and message[
        "tool_calls"
    ][0]["id"].startswith("call_")
    again, _ = translate_response(google([{"functionCall": {"name": "run", "args": {"cmd": "ls"}}}]), nr)
    assert (
        again["choices"][0]["message"]["tool_calls"][0]["id"] == message["tool_calls"][0]["id"]
    )  # identifiant déterministe


@pytest.mark.parametrize(
    "finish, expected", [("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("RECITATION", "content_filter")]
)
def test_finish_reasons(finish, expected):
    assert (
        translate_response(google([{"text": "x"}], finish=finish), norm())[0]["choices"][0]["finish_reason"] == expected
    )


def test_a_prompt_blocked_by_google_is_a_content_filter_completion_with_its_usage():
    blocked = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 7, "totalTokenCount": 7},
    }
    completion, usage = translate_response(blocked, norm())
    assert completion["choices"][0]["finish_reason"] == "content_filter" and usage.consumed_tokens == 7


@pytest.mark.parametrize(
    "response",
    [
        {"usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 1}},  # ni candidat ni blocage
        {"candidates": [{}, {}], "usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 1}},
        {
            "candidates": [{"content": {"parts": [{"functionCall": {"name": 5, "args": {}}}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 1},
        },
        google([{"text": "x"}], finish="OTHER"),
        google([{"text": "x"}], usage={}),
        google([{"text": "x"}], usage={"promptTokenCount": 7, "candidatesTokenCount": -1, "totalTokenCount": 6}),
        google([{"text": "x"}], usage={"promptTokenCount": True, "totalTokenCount": 1}),
        google([{"text": "x"}], usage={"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 11}),
        google(
            [{"text": "x"}],
            usage={"promptTokenCount": 7, "candidatesTokenCount": 3, "thoughtsTokenCount": 4, "totalTokenCount": 10},
        ),
        "pas un objet",
    ],
)
def test_malformed_responses_or_usage_are_bound_violations_never_a_silent_zero(response):
    with pytest.raises(BrokerBoundViolation):
        translate_response(response, norm())


def test_usage_arithmetic_has_no_double_count():
    usage = parse_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 10,
                "toolUsePromptTokenCount": 2,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 7,
                "totalTokenCount": 24,
            }
        }
    )
    assert (usage.consumed_tokens, usage.output_tokens, usage.total) == (24, 12, 24)


def test_count_tokens_without_a_total_is_an_explicit_refusal_not_an_estimate():
    assert parse_count_tokens({"totalTokens": 12}) == 12 and parse_count_tokens({"totalTokens": 0}) == 0
    for bad in ({}, {"totalTokens": "12"}, {"totalTokens": -1}, {"totalTokens": True}, None, []):
        with pytest.raises(BrokerUnsupported):
            parse_count_tokens(bad)
