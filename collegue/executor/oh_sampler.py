"""Échantillonneur LLM one-shot via ABONNEMENT ChatGPT (Codex) — exécuté DANS le sandbox.

Le processus principal ne peut pas appeler ``LLM.subscription_login`` (le SDK OpenHands
n'est présent que dans l'image sandbox). Ce script généralise le chemin prouvé du coder
(``oh_runner``) à un appel LLM one-shot : il lit ``{"system": ..., "prompt": ...}`` en JSON
sur stdin, fait UNE complétion via l'abonnement (modèle fort, ex. gpt-5.4, 0 coût API) et
imprime la réponse entre ``<<<SAMPLE_BEGIN>>>…<<<SAMPLE_END>>>`` (robuste au bruit/bannière).
Sert au reviewer/juge du produit (``SubscriptionSampler`` dans le ctx offline). Code != 0 sur échec.
"""

from __future__ import annotations

import json
import os
import sys


def _usage_payload(llm, model: str) -> dict:
    """Extrait les compteurs SDK hors du texte contrôlé par le modèle."""
    usage = getattr(getattr(llm, "metrics", None), "accumulated_token_usage", None)

    def value(*names: str) -> int:
        for name in names:
            candidate = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if candidate is not None:
                return max(0, int(candidate or 0))
        return 0

    return {
        "prompt_tokens": value("prompt_tokens", "input_tokens"),
        "completion_tokens": value("completion_tokens", "output_tokens"),
        "model": model,
        "billable": False,
    }


def _extract_text(resp) -> str:
    """Extraction défensive du texte d'un ``LLMResponse`` OpenHands."""
    # 1) LLMResponse.message.content (list[TextContent])
    msg = getattr(resp, "message", None)
    if msg is not None:
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, (list, tuple)):
            parts = []
            for c in content:
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
            if parts:
                return "\n".join(parts)
    # 2) litellm raw : resp.choices[0].message.content
    try:
        return resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001
        pass
    # 3) dernier recours
    return str(resp)


def main() -> int:
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent

    model = os.environ.get("LLM_MODEL", "gpt-5.4")
    # L'allow-list client (OPENAI_CODEX_MODELS) est désynchronisée du backend
    # ChatGPT (cf. oh_runner) : on l'étend avec le modèle demandé, le serveur
    # reste l'autorité (un modèle non servi lève une BadRequestError explicite).
    try:
        import openhands.sdk.llm.auth.openai as _oa

        _oa.OPENAI_CODEX_MODELS = frozenset(set(_oa.OPENAI_CODEX_MODELS) | {model})
    except Exception:  # noqa: BLE001
        pass

    data = json.load(sys.stdin)
    system = str(data.get("system", ""))
    prompt = str(data.get("prompt", ""))

    llm = LLM.subscription_login(
        vendor="openai",
        model=model,
        open_browser=False,
        service_id="sampler",
        num_retries=int(os.environ.get("OH_NUM_RETRIES", "6")),
        retry_min_wait=int(os.environ.get("OH_RETRY_MIN", "5")),
        retry_max_wait=int(os.environ.get("OH_RETRY_MAX", "60")),
        timeout=int(os.environ.get("OH_LLM_TIMEOUT", "180")),
    )
    messages = [
        Message(role="system", content=[TextContent(text=system)]),
        Message(role="user", content=[TextContent(text=prompt)]),
    ]
    # Le backend Codex/ChatGPT FORCE le streaming (self.stream=True) → completion
    # exige un on_token. On accumule les deltas (source de vérité), repli sur le
    # LLMResponse final si jamais le stream ressort vide.
    chunks: list[str] = []

    def _on_token(chunk) -> None:
        try:
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                chunks.append(piece)
        except Exception:  # noqa: BLE001
            pass

    # gpt-5.x via Codex/ChatGPT passe par l'API RESPONSES (/responses), pas
    # /chat/completions (qui renvoie 404 sur ce backend). On route dynamiquement.
    use_responses = False
    try:
        use_responses = bool(llm.uses_responses_api())
    except Exception:  # noqa: BLE001
        use_responses = False
    call = llm.responses if use_responses else llm.completion
    resp = call(messages=messages, on_token=_on_token)
    text = _extract_text(resp)
    if not (text or "").strip():
        text = "".join(chunks)
    usage_json = json.dumps(_usage_payload(llm, model), separators=(",", ":"))
    sys.stdout.write(
        "<<<SAMPLE_BEGIN>>>" + text + "<<<SAMPLE_END>>>" + "<<<SAMPLE_USAGE>>>" + usage_json + "<<<SAMPLE_USAGE_END>>>"
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"oh_sampler: échec ({exc!r})", file=sys.stderr)
        sys.exit(1)
