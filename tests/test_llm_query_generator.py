"""Unit tests for cms_entry_assistant.llm_query_generator.

LLM call は実 API を叩かず、stub client / monkeypatch で動かす。キャッシュは
専用 tmp_path に切る (CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cms_entry_assistant import llm_query_generator as gen


# ---------------------------------------------------------------------------
# Stub Anthropic client
# ---------------------------------------------------------------------------


class _TextBlock:
    """Minimal stand-in for anthropic.types.TextBlock (has a .text attribute)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    """response.content = [TextBlock(...)] のミニマル版。"""

    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _StubMessages:
    def __init__(self, payload: Any, *, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        if isinstance(self._payload, str):
            return _Response(self._payload)
        return self._payload


class _StubClient:
    def __init__(self, payload: Any, *, raise_exc: Exception | None = None) -> None:
        self.messages = _StubMessages(payload, raise_exc=raise_exc)


# ---------------------------------------------------------------------------
# parse_llm_response
# ---------------------------------------------------------------------------


def test_parse_llm_response_parses_json_object():
    payload = json.dumps(
        {
            "intent": "中国経済の停滞",
            "keywords": ["上海", "ビル群"],
            "negative_keywords": ["習近平"],
            "search_queries": ["中国 経済", "上海 高層ビル"],
            "rationale": "象徴的な都市風景",
            "confidence": 0.8,
        }
    )
    plan = gen.parse_llm_response(payload)
    assert plan.search_queries == ["中国 経済", "上海 高層ビル"]
    assert plan.intent == "中国経済の停滞"
    assert plan.keywords == ["上海", "ビル群"]
    assert plan.negative_keywords == ["習近平"]
    assert plan.rationale == "象徴的な都市風景"
    assert plan.confidence == 0.8


def test_parse_llm_response_accepts_markdown_fenced_json():
    text = '```json\n{"search_queries": ["foo", "bar"]}\n```'
    plan = gen.parse_llm_response(text)
    assert plan.search_queries == ["foo", "bar"]


def test_parse_llm_response_raises_on_missing_queries():
    with pytest.raises(ValueError):
        gen.parse_llm_response('{"intent": "no queries here"}')


# ---------------------------------------------------------------------------
# compute_slot_hash — same payload → same hash, regardless of key order
# ---------------------------------------------------------------------------


def test_compute_slot_hash_is_stable_across_key_order():
    h1 = gen.compute_slot_hash({"a": 1, "b": 2})
    h2 = gen.compute_slot_hash({"b": 2, "a": 1})
    assert h1 == h2


def test_compute_slot_hash_differs_when_payload_differs():
    h1 = gen.compute_slot_hash({"slot": "hero"})
    h2 = gen.compute_slot_hash({"slot": "h4_1"})
    assert h1 != h2


# ---------------------------------------------------------------------------
# generate_query_plan — stub client path
# ---------------------------------------------------------------------------


def _payload_json() -> str:
    return json.dumps(
        {
            "intent": "デモ",
            "search_queries": ["デモ クエリ"],
            "keywords": ["都市"],
            "negative_keywords": [],
        }
    )


def test_generate_query_plan_with_stub_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE", raising=False)
    gen.clear_cache()
    client = _StubClient(_payload_json())

    result = gen.generate_query_plan(
        {"slot_key": "hero", "primary_query": "中国 経済"},
        client=client,
    )

    assert result.ok
    assert result.plan is not None
    assert result.plan.search_queries == ["デモ クエリ"]
    assert client.messages.calls, "messages.create should have been called"


def test_generate_query_plan_uses_cache_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE", raising=False)
    gen.clear_cache()
    client = _StubClient(_payload_json())
    slot = {"slot_key": "hero", "primary_query": "中国 経済"}

    first = gen.generate_query_plan(slot, client=client)
    second = gen.generate_query_plan(slot, client=client)

    assert first.ok and second.ok
    # 1 回目で API、2 回目はキャッシュ
    assert len(client.messages.calls) == 1
    assert second.from_cache is True


def test_generate_query_plan_returns_error_when_client_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE", "1")
    client = _StubClient(None, raise_exc=RuntimeError("boom"))

    result = gen.generate_query_plan(
        {"slot_key": "hero"},
        client=client,
    )

    assert not result.ok
    assert result.plan is None
    assert "boom" in (result.error or "")


def test_generate_query_plan_returns_error_when_api_key_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = gen.generate_query_plan({"slot_key": "hero"})

    assert not result.ok
    assert result.plan is None
    assert "ANTHROPIC_API_KEY" in (result.error or "")
