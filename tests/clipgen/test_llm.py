from __future__ import annotations

import json
import sys
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from clipgen.llm import AnthropicProvider, build_prompt, polish_titles  # noqa: E402
from clipgen.titles import TitleSuggestion  # noqa: E402


@dataclass
class DummyCandidate:
    video_id: str = "v1"
    title: str = "政治家の発言が議論に"
    channel_id: str = "c1"
    channel_handle: str | None = None
    channel_title: str = "ニュース"
    published_at: datetime = field(default_factory=datetime.utcnow)
    duration_sec: int | None = 120
    view_count: int = 1000
    like_count: int | None = None
    comment_count: int | None = None
    description: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    target_format: str = "short"
    defamation_review_required: bool = False


@dataclass
class DummyHighlight:
    start_sec: float = 0
    end_sec: float = 10
    score: float = 1.0
    rationale: list[str] = field(default_factory=lambda: ["重要発言"])
    keywords: list[str] = field(default_factory=lambda: ["政策"])


class FakeProvider:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.prompt = ""
        self.max_candidates = 0

    def generate_titles(self, prompt: str, *, max_candidates: int) -> list[str]:
        self.prompt = prompt
        self.max_candidates = max_candidates
        return self.lines[:max_candidates]


def test_build_prompt_mentions_aggressiveness_and_review_clause() -> None:
    candidate = DummyCandidate(risk_flags=["DEFAMATION_REVIEW_REQUIRED"])

    prompt = build_prompt(
        candidate,
        DummyHighlight(),
        "short",
        3,
        ["発言者に波紋"],
    )

    assert "煽りはOK" in prompt
    assert "名誉毀損・誹謗中傷・うそは禁止" in prompt
    assert "事実に厳密に留めて" in prompt


def test_build_prompt_level_three_contains_strict_safety_terms() -> None:
    prompt = build_prompt(
        DummyCandidate(),
        None,
        "short",
        3,
        ["発言者に波紋"],
    )

    assert any(term in prompt for term in ("事実に反する断定", "肩書き付け", "未確定の疑惑"))


def test_build_prompt_defamation_review_required_adds_review_instruction() -> None:
    prompt = build_prompt(
        DummyCandidate(defamation_review_required=True),
        None,
        "short",
        3,
        ["発言者に波紋"],
    )

    assert "この素材は名誉毀損レビュー対象です" in prompt
    assert "事実を逸脱した煽りタイトルは出さないでください" in prompt
    assert "確定でない事項は「〜と話題」「〜と発言」止まり" in prompt


def test_build_prompt_aggressiveness_zero_is_neutral() -> None:
    prompt = build_prompt(
        DummyCandidate(),
        None,
        "long",
        0,
        ["中立タイトル"],
    )

    assert "中立" in prompt
    assert "事実関係を慎重" in prompt


def test_polish_titles_returns_original_when_provider_none() -> None:
    suggestions = [
        TitleSuggestion("発言者に波紋", "short", "neutral", ["REVIEW"]),
    ]

    result = polish_titles(
        suggestions,
        DummyCandidate(),
        None,
        provider=None,
        aggressiveness=0,
    )

    assert result is suggestions


def test_polish_titles_returns_original_when_provider_returns_empty_list() -> None:
    suggestions = [
        TitleSuggestion("発言者に波紋", "short", "neutral", ["REVIEW"]),
    ]

    result = polish_titles(
        suggestions,
        DummyCandidate(),
        None,
        provider=FakeProvider([]),
        aggressiveness=0,
    )

    assert result is suggestions


def test_polish_titles_returns_original_on_http_error_from_anthropic_provider(monkeypatch) -> None:
    suggestions = [
        TitleSuggestion("発言者に波紋", "short", "neutral", ["REVIEW"]),
    ]

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "server error",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    provider = AnthropicProvider(api_key="test-key", model="claude-opus-4-7", timeout=1)
    result = polish_titles(
        suggestions,
        DummyCandidate(),
        None,
        provider=provider,
        aggressiveness=0,
    )

    assert result is suggestions


def test_polish_titles_uses_provider_and_marks_polished() -> None:
    suggestions = [
        TitleSuggestion("発言者に波紋", "short", "neutral", []),
    ]
    provider = FakeProvider(["政策発言に広がる反応", "発言の背景に注目"])

    result = polish_titles(
        suggestions,
        DummyCandidate(),
        DummyHighlight(),
        provider=provider,
        aggressiveness=1,
    )

    assert [s.text for s in result] == ["政策発言に広がる反応", "発言の背景に注目"]
    assert all(s.style == "polished" for s in result)
    assert all("LLM_POLISHED" in s.flags for s in result)
    assert provider.max_candidates <= 5


def test_polish_titles_inherits_review_prefix_for_defamation_review_candidate() -> None:
    suggestions = [
        TitleSuggestion("[REVIEW] 発言者に波紋", "short", "neutral", ["REVIEW"]),
    ]
    provider = FakeProvider(["政策発言に広がる反応"])

    result = polish_titles(
        suggestions,
        DummyCandidate(defamation_review_required=True),
        None,
        provider=provider,
        aggressiveness=1,
    )

    assert result[0].text == "[REVIEW] 政策発言に広がる反応"
    assert "REVIEW" in result[0].flags
    assert "LLM_POLISHED" in result[0].flags


def test_polish_titles_flags_suspected_defamation_terms() -> None:
    suggestions = [
        TitleSuggestion("発言者に波紋", "short", "neutral", []),
    ]
    provider = FakeProvider(["スパイ疑惑が拡大", "発言めぐり逮捕と話題"])

    result = polish_titles(
        suggestions,
        DummyCandidate(),
        None,
        provider=provider,
        aggressiveness=3,
    )

    assert "SUSPECTED_DEFAMATION" in result[0].flags
    assert "SUSPECTED_DEFAMATION" in result[1].flags


def test_anthropic_provider_posts_with_stubbed_urlopen(monkeypatch) -> None:
    captured = {}

    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "content": [
                        {"text": "1. 政策発言に注目"},
                        {"text": "2. 議論呼ぶ発言"},
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["api_key"] = req.headers["X-api-key"]
        return DummyResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    provider = AnthropicProvider(api_key="test-key", model="claude-opus-4-7", timeout=1)
    result = provider.generate_titles("prompt", max_candidates=5)

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["timeout"] == 1
    assert captured["body"]["model"] == "claude-opus-4-7"
    assert captured["body"]["max_tokens"] <= 400
    assert captured["api_key"] == "test-key"
    assert result == ["政策発言に注目", "議論呼ぶ発言"]


def test_anthropic_provider_returns_empty_list_for_empty_content(monkeypatch) -> None:
    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"content": [{"text": ""}]}).encode("utf-8")

    def fake_urlopen(req, timeout):
        return DummyResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    provider = AnthropicProvider(api_key="test-key", model="claude-opus-4-7", timeout=1)

    assert provider.generate_titles("prompt", max_candidates=5) == []
