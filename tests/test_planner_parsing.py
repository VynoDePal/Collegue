"""Tests du fallback JSON partagé par les étapes P1/P2 du planner."""

from __future__ import annotations

import pytest

from collegue.planner._parsing import json_from_text


def test_json_from_text_ignores_json_inside_one_leading_thought():
    raw = (
        '  <ThOuGhT>Essai interne : {"title": "mauvais"}</tHoUgHt>\nRéponse finale :\n```json\n{"title": "bon"}\n```  '
    )

    assert json_from_text(raw) == {"title": "bon"}


@pytest.mark.parametrize(
    "raw",
    [
        '<thought>{"draft": true}\n{"final": true}',
        'préface <thought>{"draft": true}</thought>\n{"final": true}',
        '<thought>un</thought><thought>deux</thought>{"final": true}',
        '<thought>un</thought>{"final": true}<thought>deux</thought>',
        '</thought>{"final": true}',
        '< thought>{"draft": true}</thought>{"final": true}',
        '<thought >{"draft": true}</thought>{"final": true}',
        '<thought>{"draft": true}</thought >{"final": true}',
    ],
)
def test_json_from_text_rejects_ambiguous_thought_envelopes(raw):
    assert json_from_text(raw) is None


def test_json_from_text_preserves_legacy_first_object_fallback():
    raw = 'Option A: {"choice": "a"}\nOption B: {"choice": "b"}'

    assert json_from_text(raw) == {"choice": "a"}


def test_json_from_text_preserves_thought_literal_inside_json_string():
    raw = '{"template": "<thought> est un texte, pas une enveloppe"}'

    assert json_from_text(raw) == {"template": "<thought> est un texte, pas une enveloppe"}
