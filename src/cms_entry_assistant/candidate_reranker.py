from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import re
from typing import Any, Protocol


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


class LlmQueryPlan(Protocol):
    intent_terms: Any
    query_terms: Any
    avoid_terms: Any


@dataclass
class RankedCandidate:
    candidate: Any
    rank_raw: float
    intent_score: float
    query_score: float
    prefs_score: float
    penalty: float
    avoided_terms: list[str]
    # Policy-3 (2026-05-14): 編集部ポリシー由来の signal。
    # policy_score は -1..+1 に正規化された値、policy_reasons はデバッグ用。
    policy_score: float = 0.0
    policy_reasons: list[str] = None  # type: ignore[assignment]

    @property
    def total(self) -> float:
        return self.rank_raw

    def __post_init__(self) -> None:
        if self.policy_reasons is None:
            object.__setattr__(self, "policy_reasons", [])


def rerank_candidates(
    candidates: Iterable[Any],
    plan: LlmQueryPlan,
    prefs: Any = None,
    *,
    apply_policy: bool = True,
) -> list[RankedCandidate]:
    """Rank candidates by LLM intent/query + editor policy.

    Policy-3 (2026-05-14):
      - hard_block 判定 (笑顔 / 肖像画 / 白人 / 黒人 等) を最終リストから完全に除外。
      - policy normalized score を 0.30 の重みで scoring 式に組み込む。
      - 既存 intent (0.40) / query (0.20) / prefs (0.15) と並列。

    apply_policy=False で旧挙動 (テスト互換用)。デフォルト True。
    """
    # Policy-3 を呼び出す。photo_preferences との循環 import を避けるため lazy import。
    if apply_policy:
        from cms_entry_assistant.photo_preferences import (  # noqa: WPS433
            evaluate_editorial_people_policy,
        )

    intent_terms = set(_tokens(_get(plan, "intent_terms", [])))
    query_terms = _unique(_tokens(_query_source(plan)))
    avoid_terms = _term_entries(_get(plan, "avoid_terms", []))

    ranked: list[RankedCandidate] = []

    for candidate in candidates:
        # Hard-filter (明示違反は最終リストから除外)
        if apply_policy:
            policy_eval = evaluate_editorial_people_policy(candidate)
            if policy_eval.hard_block:
                continue
            # raw score -100..+20 を -1..+1 に正規化
            policy_norm = max(-1.0, min(1.0, policy_eval.score / 20.0))
            policy_reasons = list(policy_eval.reasons)
        else:
            policy_norm = 0.0
            policy_reasons = []

        candidate_terms = set(_candidate_tokens(candidate))
        alt_terms = set(_tokens(_get(candidate, "alt", "")))

        intent_score = _jaccard(intent_terms, candidate_terms)
        query_score = _coverage(query_terms, alt_terms)
        prefs_score = _prefs_score(prefs, candidate)

        candidate_text = " ".join(candidate_terms)
        avoided_terms = _matched_avoid_terms(avoid_terms, candidate_terms, candidate_text)
        penalty = len(avoided_terms) * 0.1

        # Policy-3 重み: 0.40 intent + 0.20 query + 0.30 policy + 0.15 prefs - penalty
        # (元 0.55 intent + 0.25 query + 0.15 prefs から intent / query を弱めて
        # policy を 0.30 で組み込む。codex policy review §1 推奨)
        rank_raw = (
            0.40 * intent_score
            + 0.20 * query_score
            + 0.30 * policy_norm
            + prefs_score  # _prefs_score 自体が既に 0.15 倍されている
            - penalty
        )

        ranked.append(
            RankedCandidate(
                candidate=candidate,
                rank_raw=rank_raw,
                intent_score=intent_score,
                query_score=query_score,
                prefs_score=prefs_score,
                penalty=penalty,
                avoided_terms=avoided_terms,
                policy_score=policy_norm,
                policy_reasons=policy_reasons,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.total,
            -item.rank_raw,
            -item.policy_score,
            -item.prefs_score,
            _detail_url(item.candidate),
        )
    )
    return ranked


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _query_source(plan: Any) -> Any:
    query_terms = _get(plan, "query_terms", None)
    if query_terms:
        return query_terms

    for name in ("query_plan", "query", "raw_query", "text"):
        value = _get(plan, name, None)
        if value:
            return value

    return []


def _candidate_tokens(candidate: Any) -> list[str]:
    tokens: list[str] = []
    tokens.extend(_tokens(_get(candidate, "alt", "")))
    tokens.extend(_tokens(_get(candidate, "title", "")))
    tokens.extend(_tokens(_get(candidate, "keywords", "")))
    return tokens


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return _TOKEN_RE.findall(value.casefold())

    if isinstance(value, bytes):
        return _tokens(value.decode("utf-8", errors="ignore"))

    if isinstance(value, Mapping):
        tokens: list[str] = []
        for item in value.values():
            tokens.extend(_tokens(item))
        return tokens

    if isinstance(value, Iterable):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_tokens(item))
        return tokens

    return _TOKEN_RE.findall(str(value).casefold())


def _term_entries(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return _unique(_tokens(value))

    if isinstance(value, bytes):
        return _term_entries(value.decode("utf-8", errors="ignore"))

    if isinstance(value, Mapping):
        entries: list[str] = []
        for item in value.values():
            entries.extend(_term_entries(item))
        return _unique(entries)

    if isinstance(value, Iterable):
        entries: list[str] = []
        for item in value:
            item_tokens = _tokens(item)
            if item_tokens:
                entries.append(" ".join(item_tokens))
        return _unique(entries)

    return _unique(_tokens(value))


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []

    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique_values.append(value)

    return unique_values


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0

    union = left | right
    if not union:
        return 0.0

    return len(left & right) / len(union)


def _coverage(needles: list[str], haystack: set[str]) -> float:
    if not needles:
        return 0.0

    hits = sum(1 for term in needles if term in haystack)
    return hits / len(needles)


def _prefs_score(prefs: Any, candidate: Any) -> float:
    score_hit = getattr(prefs, "score_hit", None)
    if not callable(score_hit):
        return 0.0

    try:
        raw_score = float(score_hit(candidate))
    except (TypeError, ValueError):
        return 0.0

    if not math.isfinite(raw_score):
        return 0.0

    return 0.15 * min(max(raw_score, 0.0), 1.0)


def _matched_avoid_terms(
    avoid_terms: list[str],
    candidate_terms: set[str],
    candidate_text: str,
) -> list[str]:
    matched: list[str] = []

    for term in avoid_terms:
        term_tokens = term.split()
        if not term_tokens:
            continue

        if len(term_tokens) == 1:
            if term_tokens[0] in candidate_terms:
                matched.append(term)
            continue

        phrase = " ".join(term_tokens)
        if phrase in candidate_text or all(token in candidate_terms for token in term_tokens):
            matched.append(term)

    return matched


def _detail_url(candidate: Any) -> str:
    value = _get(candidate, "detail_url", "")
    if value is None:
        return ""
    return str(value).casefold()


__all__ = ["RankedCandidate", "rerank_candidates"]

