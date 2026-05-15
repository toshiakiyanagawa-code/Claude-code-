"""Measure how close our top candidates are to the editor's actual choice.

For each (article, slot) pair:
  - candidate_alts: top-5 alt texts from our pipeline
  - actual_alts: alt texts of the published photos (filtered to iStock-sourced)
  - similarity: token-set overlap (Jaccard) between each candidate alt and the
    best-matching actual alt

Aggregates:
  - top-1 / top-5 jaccard mean
  - "hit rate" = candidate has >= threshold similarity to ANY actual
  - per-topic breakdown
  - bottom-quartile gaps (slots where top-5 vs actual = 0 similarity)
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

_TOKEN_RE = re.compile(r"[一-龠ぁ-んァ-ヶー]{2,}|[A-Za-z]{3,}")

_STOPWORDS = {
    # 撮影一般語 (alt の定型句)
    "ストックフォト",
    "画像",
    "イメージ",
    "写真",
    "image",
    "stock",
    "photo",
    "pictures",
    "photograph",
    # 日本語助詞 / 連体修飾語っぽいフラグメント
    "する",
    "して",
    "ある",
    "いる",
    "から",
    "など",
    "それ",
    "これ",
}


def tokens(text: str) -> set[str]:
    """Tokenize Japanese / Latin words 2+ chars, strip stopwords."""
    raw = _TOKEN_RE.findall(text or "")
    return {t.lower() for t in raw if t.lower() not in _STOPWORDS and len(t) >= 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_TOPIC_RULES = [
    ("中国", ("中国", "習近平", "毛沢東")),
    ("戦争", ("戦争", "原爆", "降伏", "玉音")),
    ("健康", ("医", "健康", "体", "寿命", "脳", "酸化", "シニア", "高齢", "長寿", "予防", "病")),
    ("教育", ("不登校", "学校", "子ども", "生徒")),
    ("ライフ", ("60代", "40代", "女", "銀座", "ママ", "結婚", "離婚")),
    ("食", ("食", "みかん", "果物", "野菜", "コーヒー")),
    ("中世", ("処刑", "中世")),
    ("ビジネス", ("再開発", "経営", "投資")),
]


def topic_of(title: str) -> str:
    t = title or ""
    for label, keywords in _TOPIC_RULES:
        if any(k in t for k in keywords):
            return label
    return "その他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help="jaccard threshold for 'close enough' hit",
    )
    args = ap.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))

    rows: list[dict] = []  # per slot+actual pair
    miss_examples: list[dict] = []
    big_hit_examples: list[dict] = []
    photographer_matches: list[dict] = []
    # Per-article photographer surfacing stats (codex review §1 retrieval ceiling)
    surfacing: list[dict] = []

    for a in data["articles"]:
        topic = topic_of(a.get("title", ""))
        # Only compare to iStock-sourced actual photos (we can't replicate 報道)
        istock_actuals = [
            img
            for img in a.get("actual", [])
            if img.get("source") == "iStock"
        ]
        if not istock_actuals:
            continue

        # Photographer set for this article (the editor's chosen iStock photographers)
        actual_photographers = {
            (img.get("photographer") or "").strip()
            for img in istock_actuals
        }
        actual_photographers.discard("")

        for s in a["suggestions"]:
            if not s["candidates"]:
                continue
            cand_alts = [c.get("alt", "") for c in s["candidates"][:5]]
            cand_photographers = [
                (c.get("photographer") or "").strip()
                for c in s["candidates"][:5]
            ]
            cand_token_sets = [tokens(alt) for alt in cand_alts]

            # Photographer match: did we surface a candidate by the same iStock
            # photographer the editor ended up choosing? Most precise signal.
            for rank, photog in enumerate(cand_photographers, start=1):
                if photog and photog in actual_photographers:
                    photographer_matches.append(
                        {
                            "topic": topic,
                            "file": a["file"],
                            "slot": s["slot_key"],
                            "photographer": photog,
                            "rank": rank,
                            "candidate_alt": cand_alts[rank - 1],
                        }
                    )
                    break  # one match per slot is enough

            # Track all photographers seen anywhere in this article for the
            # retrieval-ceiling proxy after the per-slot loop.
            for photog in cand_photographers:
                if photog:
                    a.setdefault("_all_photographers", set()).add(photog)
                    a.setdefault("_total_top5_candidates", 0)
                    a["_total_top5_candidates"] += 1

            # Body-only alt similarity (hero alt is the article title, not the
            # iStock alt, so it heavily distorts the metric).
            for actual in istock_actuals:
                if actual.get("role") != "body":
                    continue
                actual_alt = actual.get("alt", "")
                actual_tokens = tokens(actual_alt)
                if not actual_tokens:
                    continue
                sims = [jaccard(actual_tokens, c) for c in cand_token_sets]
                top1 = sims[0]
                top5_max = max(sims)
                rows.append(
                    {
                        "topic": topic,
                        "file": a["file"],
                        "slot": s["slot_key"],
                        "actual_role": actual.get("role", ""),
                        "actual_alt": actual_alt,
                        "actual_photographer": actual.get("photographer", ""),
                        "top1": top1,
                        "top5_max": top5_max,
                        "candidate_alts": cand_alts,
                    }
                )

                if top5_max < 0.05:
                    miss_examples.append(rows[-1])
                elif top5_max > 0.30:
                    big_hit_examples.append(rows[-1])

        # Article-level surfacing summary (retrieval ceiling proxy).
        # 「actual photographer が全 slot の top-5 union のどこかに登場したか」
        # を見る。slot 別 ranker の問題か、そもそも検索到達できないかの分離。
        union_photogs = a.get("_all_photographers", set())
        total_pool = a.get("_total_top5_candidates", 0)
        reachable_in_top5 = [p for p in actual_photographers if p in union_photogs]
        surfacing.append(
            {
                "file": a["file"],
                "topic": topic,
                "actual_photogs": list(actual_photographers),
                "n_actual_photogs": len(actual_photographers),
                "n_reachable_in_top5_union": len(reachable_in_top5),
                "reachable_photogs": reachable_in_top5,
                "union_size": len(union_photogs),
                "top5_total_candidates": total_pool,
            }
        )

    # Distinct articles with at least one iStock actual photographer
    eligible_articles = sum(
        1
        for a in data["articles"]
        if any(
            (img.get("photographer") or "").strip() and img.get("source") == "iStock"
            for img in a.get("actual", [])
        )
    )

    overall = {
        "total_pairs": len(rows),
        "top1_mean": mean(r["top1"] for r in rows) if rows else 0,
        "top5_max_mean": mean(r["top5_max"] for r in rows) if rows else 0,
        "top1_hit_rate": (sum(1 for r in rows if r["top1"] >= args.threshold) / len(rows)) if rows else 0,
        "top5_hit_rate": (sum(1 for r in rows if r["top5_max"] >= args.threshold) / len(rows)) if rows else 0,
        "near_miss_rate": (sum(1 for r in rows if r["top5_max"] < 0.05) / len(rows)) if rows else 0,
        "photographer_match_count": len(photographer_matches),
        "eligible_articles_for_photog_match": eligible_articles,
    }

    by_topic = defaultdict(list)
    for r in rows:
        by_topic[r["topic"]].append(r)
    topic_stats = {}
    for t, items in by_topic.items():
        topic_stats[t] = {
            "n": len(items),
            "top5_max_mean": round(mean(r["top5_max"] for r in items), 3),
            "top5_hit_rate": round(
                sum(1 for r in items if r["top5_max"] >= args.threshold) / len(items),
                3,
            ),
        }

    # Build a markdown report
    lines: list[str] = []
    lines.append(f"# Precision analysis: {args.input.name}")
    lines.append("")
    lines.append("## Photographer match (most precise signal)")
    lines.append("")
    lines.append(
        f"- Articles with iStock photographer recorded: **{overall['eligible_articles_for_photog_match']}**"
    )
    lines.append(
        f"- Slots where one of top-5 candidates was the SAME photographer as the editor's choice: **{overall['photographer_match_count']}**"
    )
    if photographer_matches:
        rank_counter = Counter(m["rank"] for m in photographer_matches)
        lines.append(f"- Rank distribution of those matches: {dict(rank_counter)}")
    lines.append("")
    lines.append("## Retrieval ceiling (codex review § 1-2)")
    lines.append("")
    eligible_surf = [s for s in surfacing if s["n_actual_photogs"] > 0]
    if eligible_surf:
        reached = sum(1 for s in eligible_surf if s["n_reachable_in_top5_union"] > 0)
        total = len(eligible_surf)
        # Random baseline: prob actual photog appears in ANY top-5 slot
        # if union has size U and total candidate count is C, expected hits per
        # actual photog ≈ min(1, U / C * 5 slots * 5 candidates), but bounded.
        # Approximation: expected = 5 / pool_estimate; we'll just report
        # observed vs total to flag if photogs are reachable at all.
        lines.append(f"- Articles analyzed (with iStock photog): **{total}**")
        lines.append(
            f"- Articles where actual photog appeared in top-5 of *some* slot: **{reached}/{total}** "
            f"({reached/total*100:.0f}%)"
        )
        lines.append("")
        lines.append("If this number is very low, the issue is *retrieval* (LLM queries / "
                     "iStock search are not surfacing the photographer at all). "
                     "If it's high but per-slot photographer_match is 0, the issue is "
                     "*ranker* (the photog was surfaced somewhere but not in the right slot).")
    lines.append("")
    lines.append("## Body-image alt similarity (jaccard)")
    lines.append("")
    lines.append(f"Body image pairs analyzed: **{overall['total_pairs']}**")
    lines.append("")
    lines.append(f"- top1 jaccard mean: **{overall['top1_mean']:.3f}**")
    lines.append(f"- top5_max jaccard mean: **{overall['top5_max_mean']:.3f}**")
    lines.append(
        f"- top1 hit rate (≥ {args.threshold}): **{overall['top1_hit_rate']*100:.1f}%**"
    )
    lines.append(
        f"- top5 hit rate (≥ {args.threshold}): **{overall['top5_hit_rate']*100:.1f}%**"
    )
    lines.append(f"- near-miss rate (top5 < 0.05): **{overall['near_miss_rate']*100:.1f}%**")
    lines.append("")
    lines.append("## By topic")
    lines.append("")
    lines.append("| topic | n | top5_max_mean | top5_hit_rate |")
    lines.append("|---|---|---|---|")
    for t, st in sorted(topic_stats.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"| {t} | {st['n']} | {st['top5_max_mean']} | {st['top5_hit_rate']*100:.1f}% |")
    lines.append("")

    lines.append("## Near misses (top5_max < 0.05) — gap targets")
    lines.append("")
    for ex in miss_examples[:15]:
        lines.append(f"### [{ex['topic']}] slot {ex['slot']} — actual({ex['actual_role']}): {ex['actual_alt'][:80]}")
        for alt in ex["candidate_alts"]:
            lines.append(f"  - {alt[:100]}")
        lines.append("")

    lines.append("## Big hits (top5_max > 0.30) — what works")
    lines.append("")
    for ex in big_hit_examples[:10]:
        lines.append(f"### [{ex['topic']}] slot {ex['slot']} — actual({ex['actual_role']}): {ex['actual_alt'][:80]}")
        for alt in ex["candidate_alts"]:
            lines.append(f"  - {alt[:100]}")
        lines.append("")

    md = "\n".join(lines)
    if args.out:
        args.out.write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
