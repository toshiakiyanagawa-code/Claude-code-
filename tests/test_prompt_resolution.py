"""Tests for the glossary auto-merge in resolve_prompt_and_hotwords (P0-H)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # jobs.py imports nothing FastAPI, but matches repo convention

from podedit.server.jobs import DEFAULT_JA_PODCAST_PROMPT, resolve_prompt_and_hotwords


def test_none_prompt_empty_glossary_keeps_default():
    """No glossary => default prompt, no hotwords. Behavior unchanged from pre-H."""
    prompt, hw, count = resolve_prompt_and_hotwords(
        initial_prompt=None, hotwords=None, glossary_terms=[]
    )
    assert prompt == DEFAULT_JA_PODCAST_PROMPT
    assert hw is None
    assert count == 0


def test_none_prompt_with_glossary_appends_and_fills_hotwords():
    prompt, hw, count = resolve_prompt_and_hotwords(
        initial_prompt=None,
        hotwords=None,
        glossary_terms=["クロード", "Anthropic"],
    )
    assert prompt.startswith(DEFAULT_JA_PODCAST_PROMPT)
    assert "固有名詞:" in prompt
    assert "クロード" in prompt
    assert hw == "クロード, Anthropic"
    assert count == 2


def test_empty_string_prompt_does_not_inject_glossary():
    """Caller explicitly asked for raw decoding — leave it alone."""
    prompt, hw, count = resolve_prompt_and_hotwords(
        initial_prompt="",
        hotwords=None,
        glossary_terms=["クロード"],
    )
    assert prompt == ""
    assert hw is None  # not auto-filled, since prompt was explicit
    assert count == 0


def test_user_prompt_overrides_glossary_merge():
    """Custom prompt => glossary not appended. Caller fully owns the prompt."""
    prompt, hw, count = resolve_prompt_and_hotwords(
        initial_prompt="日本語の対談",
        hotwords=None,
        glossary_terms=["クロード"],
    )
    assert prompt == "日本語の対談"
    assert hw is None
    assert count == 0


def test_caller_supplied_hotwords_preserved():
    """If caller already supplied hotwords, glossary does not overwrite it."""
    prompt, hw, count = resolve_prompt_and_hotwords(
        initial_prompt=None,
        hotwords="custom, terms",
        glossary_terms=["クロード"],
    )
    # Glossary still goes into the prompt — but not hotwords.
    assert "クロード" in prompt
    assert hw == "custom, terms"
    assert count == 1
