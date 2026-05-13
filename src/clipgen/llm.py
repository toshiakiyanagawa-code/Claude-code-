from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

from .titles import TitleSuggestion


class LLMProvider(Protocol):
    def generate_titles(self, prompt: str, *, max_candidates: int) -> list[str]:
        ...


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "claude-opus-4-7",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.timeout = timeout

    def generate_titles(self, prompt: str, *, max_candidates: int) -> list[str]:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        max_candidates = max(1, min(5, max_candidates))
        payload = {
            "model": self.model,
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")

            data = json.loads(raw)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            TimeoutError,
            OSError,
        ):
            return []

        content = data.get("content") or []
        if not content:
            return []

        text = "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        ).strip()
        if not text:
            return []

        return _split_title_lines(text)[:max_candidates]


def build_prompt(
    candidate,
    highlight,
    target_format: str,
    aggressiveness: int,
    base_titles: list[str],
) -> str:
    level = max(0, min(3, aggressiveness))
    tone = "中立で、事実関係を慎重に扱ってください。"
    if level == 1:
        tone = "少し引きのある自然な表現にしてください。"
    elif level == 2:
        tone = "興味を引く表現にしてよいですが、誇張は避けてください。"
    elif level == 3:
        tone = (
            "煽りはOKですが、名誉毀損・誹謗中傷・うそは禁止です。"
            "ただし事実に反する断定、人物への犯罪・スパイ・反社などの肩書き付け、"
            "未確定の疑惑を確定として書くことは禁止です。"
        )

    review = ""
    if _defamation_review_required(candidate):
        review = (
            "\n念のため慎重に、事実に厳密に留めてください。"
            "\nこの素材は名誉毀損レビュー対象です。"
            "事実を逸脱した煽りタイトルは出さないでください。"
            "確定でない事項は「〜と話題」「〜と発言」止まりにしてください。"
        )

    highlight_text = ""
    if highlight is not None:
        keywords = getattr(highlight, "keywords", None) or []
        rationale = getattr(highlight, "rationale", None) or []
        if keywords:
            highlight_text += "\n注目キーワード: " + "、".join(str(x) for x in keywords[:5])
        if rationale:
            highlight_text += "\n注目理由: " + " / ".join(str(x) for x in rationale[:3])

    base = "\n".join(f"- {title}" for title in base_titles[:5])
    return (
        "以下のテンプレタイトルを参考に、同義語や誤表記を避けた自然なタイトルを2、3個提案してください。\n"
        "出力はタイトルのみを改行区切りにしてください。番号、説明、引用符は不要です。\n"
        f"対象形式: {target_format}\n"
        f"元動画タイトル: {getattr(candidate, 'title', '')}\n"
        f"チャンネル: {getattr(candidate, 'channel_title', '')}"
        f"{highlight_text}\n"
        f"表現方針: {tone}"
        f"{review}\n"
        "名誉毀損・誹謗中傷・事実に反する断定・断定できない犯罪やスパイ等の肩書き付け・未確定の疑惑・虚偽は禁止です。\n"
        "テンプレタイトル:\n"
        f"{base}"
    )


def polish_titles(
    suggestions: list[TitleSuggestion],
    candidate,
    highlight,
    *,
    provider: LLMProvider | None,
    aggressiveness: int,
) -> list[TitleSuggestion]:
    if provider is None:
        return suggestions

    base_titles = [s.text for s in suggestions]
    target_format = (
        suggestions[0].format
        if suggestions
        else getattr(candidate, "target_format", "short")
    )
    prompt = build_prompt(
        candidate,
        highlight,
        target_format,
        aggressiveness,
        base_titles,
    )

    try:
        generated = provider.generate_titles(
            prompt,
            max_candidates=min(5, max(3, len(suggestions))),
        )
    except Exception:
        return suggestions

    titles = _filter_generated_titles(generated)
    if not titles:
        return suggestions

    base_flags = suggestions[0].flags if suggestions else []
    review_prefix = any(s.text.startswith("[REVIEW] ") for s in suggestions)
    out: list[TitleSuggestion] = []
    for title in titles[:5]:
        if review_prefix and not title.startswith("[REVIEW] "):
            title = "[REVIEW] " + title
        flags = list(dict.fromkeys([*base_flags, "LLM_POLISHED"]))
        if _suspected_defamation(title):
            flags.append("SUSPECTED_DEFAMATION")
        if _has_repetition(title):
            flags.append("REPETITIVE")
        out.append(
            TitleSuggestion(
                text=title,
                format=target_format,
                style="polished",
                flags=flags,
            )
        )
    return out


def _split_title_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_title_line(line)
        if cleaned:
            out.append(cleaned)
    return out


def _clean_title_line(line: str) -> str:
    line = line.strip()
    while line and line[0] in "-*・0123456789.、)） ":
        line = line[1:].strip()
    return line.strip("\"'「」")


def _filter_generated_titles(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        title = _clean_title_line(line)
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(title)
    return out


def _defamation_review_required(candidate) -> bool:
    if bool(getattr(candidate, "defamation_review_required", False)):
        return True
    flags = getattr(candidate, "risk_flags", None) or []
    joined = " ".join(str(flag).lower() for flag in flags)
    return "defamation" in joined or "名誉毀損" in joined


def _suspected_defamation(text: str) -> bool:
    terms = (
        "犯罪者",
        "犯人",
        "詐欺師",
        "スパイ",
        "売国奴",
        "反社",
        "工作員",
        "疑惑",
        "逮捕",
        "容疑",
        "黒幕",
        "主犯",
        "凶悪",
        "悪事",
        "不正",
    )
    return any(term in text for term in terms)


def _has_repetition(text: str) -> bool:
    tokens = [part for part in text.replace("　", " ").split(" ") if part]
    if len(tokens) != len(set(tokens)):
        return True

    for size in range(2, min(8, len(text) // 2 + 1)):
        for i in range(0, len(text) - size * 2 + 1):
            if text[i : i + size] == text[i + size : i + size * 2]:
                return True
    return False
