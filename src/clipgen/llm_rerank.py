"""Claude API でハイライト候補を rerank する.

2026-05-14 codex review 後 v2:
- temperature=0 で決定論的に
- publishable は厳格 (is True のみ採用)
- llm_score を 0..100 にクランプ
- 各候補に pre/post 文脈 (前後 15 秒) を添えて、文脈安全を判定可能にする
- ID 欠落・重複・型エラーをすべて握って fallback

コスト (Haiku 4.5): 約 $0.085/110 分動画、Sonnet 4.6 は約 $0.255。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from .highlights import Cue
from .political_scoring import WindowFeatures, text_in_window

DEFAULT_MODEL = "claude-haiku-4-5"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


@dataclass
class LLMRerankResult:
    start_sec: float
    end_sec: float
    llm_score: float  # 0-100 (clamped)
    reasoning: str
    publishable: bool


def _build_prompt(
    features: list[WindowFeatures],
    *,
    program_title: str,
    target_format: str,
    cues: list[Cue] | None = None,
    context_sec: float = 15.0,
) -> str:
    """LLM に渡すユーザーメッセージを構築. cues を渡せば pre/post 文脈を入れる."""
    fmt_desc = (
        "YouTube Shorts (60 秒以内、縦動画、ホーム フィードで素早くスクロール中の視聴者にフックする必要)"
        if target_format == "short"
        else "YouTube 通常動画 (8-12 分、ホーム フィードで興味を持って開く視聴者向け、本格的な解説)"
    )
    candidates = []
    for i, w in enumerate(features, start=1):
        candidate = {
            "id": i,
            "start_sec": round(w.start_sec, 1),
            "end_sec": round(w.end_sec, 1),
            "duration_sec": round(w.duration_sec, 1),
            "composite_score": round(w.composite_score(), 3),
            "political_density": round(w.political_density, 2),
            "matched_politicians": w.matched_politicians[:10],
            "matched_parties": w.matched_parties[:10],
            "matched_policies": w.matched_policies[:10],
            "matched_numbers": w.matched_numbers[:10],
            "matched_admin_hard": w.matched_admin_hard[:10],
            "matched_admin_soft": w.matched_admin_soft[:10],
            "head_text": w.head_text[:300],
            "text": w.text[:1200],
        }
        if cues:
            candidate["pre_context"] = text_in_window(cues, w.start_sec - context_sec, w.start_sec)[:400]
            candidate["post_context"] = text_in_window(cues, w.end_sec, w.end_sec + context_sec)[:400]
        candidates.append(candidate)

    return (
        f"あなたは政治系切り抜き YouTube チャンネルの編集者です。\n"
        f"以下の番組から、{fmt_desc}用の切り抜き候補が複数あります。\n"
        f"各候補について、視聴回数が伸びる可能性を 0-100 で採点してください。\n\n"
        f"番組: {program_title}\n\n"
        f"## 採点軸\n"
        f"- 政治的重要度 (固有名詞・数字・政策語の密度)\n"
        f"- フック性 (冒頭 3-5 秒で結論や対立が立つか、誰が誰に何を言ったか即時理解可能か)\n"
        f"- 対立構造 (反論、批判、断定的発言)\n"
        f"- 具体性 (金額、組織名、固有名詞)\n"
        f"- 意外性・新規性 (速報・初出)\n"
        f"- 文脈安全 (pre_context / post_context を見て、切り抜きで誤読しないか、誇大広告にならないか)\n"
        f"- 配信運営の雑談・告知部分(matched_admin_hard が多い)は強くペナルティ\n\n"
        f"## 候補窓\n"
        f"```json\n{json.dumps(candidates, ensure_ascii=False, indent=1)}\n```\n\n"
        f"## 出力形式\n"
        f"JSON 配列のみを返してください。テキスト・前置きは不要。各要素:\n"
        f'{{"id": <int>, "llm_score": <number 0-100>, "publishable": <true|false>, "reasoning": "<短い理由 40 字以内>"}}\n'
        f"id 順ではなく llm_score 降順で並べてください。\n"
        f"`publishable` は文脈安全 + 誇大広告でない + 発言主体が明確のすべてを満たすときのみ true。"
    )


def call_claude(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4000,
    timeout: float = 60.0,
) -> str:
    """Anthropic Messages API を urllib で呼ぶ. temperature=0 で決定論的に."""
    body = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _API_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    blocks = payload.get("content", [])
    for blk in blocks:
        if blk.get("type") == "text":
            return blk["text"]
    raise RuntimeError("Claude response had no text block")


def _strict_bool(v) -> bool:
    """JSON の bool 厳格判定。文字列 'false' を False、True/'true' を True とする."""
    if v is True:
        return True
    if v is False:
        return False
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return False


def _parse_llm_response(
    response_text: str, features: list[WindowFeatures]
) -> list[LLMRerankResult]:
    """LLM 出力 JSON 配列を厳格にパース."""
    start = response_text.find("[")
    end = response_text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("LLM response did not contain a JSON array")
    rows = json.loads(response_text[start : end + 1])
    if not isinstance(rows, list):
        raise ValueError("LLM response was not a JSON array")
    seen_ids: set[int] = set()
    results: list[LLMRerankResult] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("id", 0)) - 1
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= len(features) or i in seen_ids:
            continue
        seen_ids.add(i)
        try:
            score = float(r.get("llm_score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))
        publishable = _strict_bool(r.get("publishable"))
        reasoning = str(r.get("reasoning", ""))[:80]
        w = features[i]
        results.append(
            LLMRerankResult(
                start_sec=w.start_sec,
                end_sec=w.end_sec,
                llm_score=score,
                reasoning=reasoning,
                publishable=publishable,
            )
        )
    return results


def rerank_candidates(
    features: list[WindowFeatures],
    *,
    program_title: str,
    target_format: str,
    top_k: int = 12,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    cues: list[Cue] | None = None,
) -> list[LLMRerankResult] | None:
    """上位 top_k 候補を LLM に渡して rerank。失敗時は None。"""
    if not features:
        return []
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None

    sorted_features = sorted(features, key=lambda w: w.composite_score(), reverse=True)
    candidates = sorted_features[:top_k]
    prompt = _build_prompt(
        candidates,
        program_title=program_title,
        target_format=target_format,
        cues=cues,
    )
    try:
        text = call_claude(prompt, api_key=key, model=model)
        return _parse_llm_response(text, candidates)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, RuntimeError) as e:
        import sys

        print(
            f"warning: LLM rerank failed ({type(e).__name__}: {e}); falling back to deterministic score",
            file=sys.stderr,
        )
        return None
