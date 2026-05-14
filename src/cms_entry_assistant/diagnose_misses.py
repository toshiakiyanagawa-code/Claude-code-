"""Diagnose where the current photo-rule engine misses the actual photo selection.

For each matched manuscript:
  1. Run build_suggestion() with the current rules
  2. Run istock_crawler.crawl_search() on the suggested query (cache only)
  3. Compare the candidate alts against the actual published image alt
  4. Compute Jaccard 2-gram similarity (the v3.1 evaluation metric)
  5. Output a list of (manuscript, actual_alt, suggested_query, top_candidate_alt,
     jaccard, miss_reason)

Run:
    uv run python -m cms_entry_assistant.diagnose_misses
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_docx
from cms_entry_assistant.instruction_parser import derive_from_manuscript

MANUSCRIPT_DIR = Path("data/manuscripts")
PUBLISHED_PATH = Path("data/published_articles.json")
CACHE_PATH = Path("data/istock_search_cache.json")
OUT_PATH = Path("output/cms_entry_assistant/diagnose_misses.json")


_ISTOCK_ALT_TAIL = re.compile(r"\s+-\s+.*ストックフォトと画像\s*$")


def _clean_candidate_alt(s: str) -> str:
    """iStock candidate alts end with ' - <query> ストックフォトと画像'. Drop it."""
    return _ISTOCK_ALT_TAIL.sub("", s or "").strip()


def _grams(s: str) -> set[str]:
    s = re.sub(r"[\s　【】『』「」［］()（）/／・|｜!！?？…：:\"”“']+", "", s or "")
    return {s[i : i + 2] for i in range(len(s) - 1)}


def jaccard(a: str, b: str) -> float:
    A, B = _grams(a), _grams(_clean_candidate_alt(b))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _cache_key(query: str) -> str:
    return (query or "").strip().lower()


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


def _candidates_for(query: str, cache: dict) -> list[dict]:
    entry = cache.get(_cache_key(query)) or {}
    return entry.get("hits", []) or []


def _flatten_actual(article: dict) -> list[dict]:
    out = []
    seen = set()
    for p in article.get("pages", []):
        for img in p.get("images", []):
            src = img.get("src", "")
            if not src or src in seen:
                continue
            seen.add(src)
            out.append(img)
    return out


def diagnose() -> list[dict]:
    pub = json.loads(PUBLISHED_PATH.read_text(encoding="utf-8"))
    cache = _load_cache()
    results: list[dict] = []
    for file_name, art in pub.items():
        if art.get("error") or "pages" not in art:
            continue
        docx_path = MANUSCRIPT_DIR / file_name
        if not docx_path.exists():
            continue
        try:
            ms = parse_docx(docx_path)
        except Exception:
            continue
        try:
            draft = convert(
                ms,
                derive_from_manuscript(ms),
                config=ConversionConfig(allow_network=False),
            )
        except Exception as exc:
            results.append({"file": file_name, "error": f"convert failed: {exc}"})
            continue
        actual_imgs = _flatten_actual(art)
        if not actual_imgs:
            continue
        title = art.get("manuscript_title", "") or art.get("published_title", "")
        hero_actuals = [i for i in actual_imgs if i.get("role") == "hero"]
        body_actuals = [i for i in actual_imgs if i.get("role") == "body"]
        for suggestion in draft.photo_suggestions[:5]:
            slot_key = suggestion.slot_key
            if slot_key == "hero":
                actual = hero_actuals[0] if hero_actuals else None
            elif slot_key.startswith("h4_"):
                try:
                    idx = int(slot_key.split("_", 1)[1]) - 1
                except ValueError:
                    idx = -1
                actual = body_actuals[idx] if 0 <= idx < len(body_actuals) else None
            else:
                actual = None
            actual_alt = (actual.get("alt") if actual else "") or ""
            actual_caption = (actual.get("caption") if actual else "") or ""
            actual_source = (actual.get("source") if actual else "") or ""
            cands = _candidates_for(suggestion.query_ja, cache)
            top_alts = [c.get("alt", "") for c in cands[:5]]
            best_jaccard = max((jaccard(actual_alt, a) for a in top_alts), default=0.0)
            if not cands:
                miss_reason = "no candidates in cache"
            elif best_jaccard >= 0.4:
                miss_reason = "ok"
            else:
                miss_reason = "candidates miss"
            results.append({
                "file": file_name,
                "slot_key": slot_key,
                "slot_label": suggestion.slot_label,
                "manuscript_title": title,
                "actual_alt": actual_alt,
                "actual_caption": actual_caption,
                "actual_source": actual_source,
                "suggested_query_ja": suggestion.query_ja,
                "suggested_query_en": suggestion.query_en,
                "type_code": suggestion.type_code,
                "rationale": suggestion.rationale,
                "top_candidate_alts": top_alts,
                "best_jaccard": round(best_jaccard, 3),
                "miss_reason": miss_reason,
            })
    return results


def main() -> None:
    results = diagnose()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(results)
    ok = sum(1 for r in results if r["miss_reason"] == "ok")
    miss = sum(1 for r in results if r["miss_reason"] == "candidates miss")
    no_cand = sum(1 for r in results if r["miss_reason"] == "no candidates in cache")
    report_lines = [
        f"診断完了: {OUT_PATH}",
        f"スロット総数: {total}",
        f"  - マッチ (Jaccard >= 0.4): {ok} ({100*ok/max(1,total):.0f}%)",
        f"  - 候補ミス: {miss} ({100*miss/max(1,total):.0f}%)",
        f"  - 候補ゼロ(キャッシュなし): {no_cand}",
    ]
    print("\n".join(report_lines))


if __name__ == "__main__":
    main()
