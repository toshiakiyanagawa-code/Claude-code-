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

    @property
    def total(self) -> float:
        return self.rank_raw


def rerank_candidates(
    candidates: Iterable[Any],
    plan: LlmQueryPlan,
    prefs: Any = None,
) -> list[RankedCandidate]:
    intent_terms = set(_tokens(_get(plan, "intent_terms", [])))
    query_terms = _unique(_tokens(_query_source(plan)))
    avoid_terms = _term_entries(_get(plan, "avoid_terms", []))

    ranked: list[RankedCandidate] = []

    for candidate in candidates:
        candidate_terms = set(_candidate_tokens(candidate))
        alt_terms = set(_tokens(_get(candidate, "alt", "")))

        intent_score = _jaccard(intent_terms, candidate_terms)
        query_score = _coverage(query_terms, alt_terms)
        prefs_score = _prefs_score(prefs, candidate)

        candidate_text = " ".join(candidate_terms)
        avoided_terms = _matched_avoid_terms(avoid_terms, candidate_terms, candidate_text)
        penalty = len(avoided_terms) * 0.1

        rank_raw = (0.55 * intent_score) + (0.25 * query_score) + prefs_score - penalty

        ranked.append(
            RankedCandidate(
                candidate=candidate,
                rank_raw=rank_raw,
                intent_score=intent_score,
                query_score=query_score,
                prefs_score=prefs_score,
                penalty=penalty,
                avoided_terms=avoided_terms,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.total,
            -item.rank_raw,
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

