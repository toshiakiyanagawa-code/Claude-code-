"""Summarize an eval JSON (cms_photo_eval output) into a codex-reviewable markdown.

Skips empty slots, groups articles by topic, and labels candidates with
heuristic policy/quality flags so codex can spot recurring failure modes.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ---- topic classifier (rough; same set as used in dev notes) ----
_TOPIC_RULES = [
    ("中国", ("中国", "習近平", "毛沢東", "上海", "北京")),
    ("戦争", ("戦争", "原爆", "日本軍", "降伏", "玉音")),
    ("健康", ("医", "健康", "体", "寿命", "脳", "酸化", "シニア", "高齢", "長寿", "さび", "予防", "病", "歯", "睡眠")),
    ("教育", ("不登校", "学校", "子ども", "生徒", "学生", "教育")),
    ("ライフ", ("60代", "40代", "女", "銀座", "ママ", "夫婦", "結婚", "離婚")),
    ("食", ("果物", "食", "みかん", "ブルーベリー", "野菜", "牛乳", "コーヒー", "酒", "ワイン", "肉")),
    ("中世", ("処刑", "中世")),
    ("ビジネス", ("再開発", "AIM", "宮崎", "経営", "起業", "投資")),
]


def topic_of(title: str) -> str:
    t = title or ""
    for label, keywords in _TOPIC_RULES:
        if any(k in t for k in keywords):
            return label
    return "その他"


# ---- candidate quality heuristics (for codex to spot patterns) ----
_TOPIC_FIT_LOOSE = (
    "外国",
    "西洋",
    "ヨーロッパ",
    "欧州",
    "アメリカ",
    "アフリカ",
    "リベリア",
    "ニュージーランド",
)


def candidate_flags(alt: str, slot_label: str, primary_query: str) -> list[str]:
    flags = []
    alt_l = (alt or "").lower()
    if any(t in alt for t in _TOPIC_FIT_LOOSE):
        flags.append("topic-drift-foreign")
    # 一見「成熟したビジネスマン」など年齢曖昧
    if "成熟" in alt or "mature" in alt_l:
        flags.append("age-ambiguous")
    # 「白い背景」スタジオ写真 — 抽象的ではあるが「外国人 white background」リスク
    if "白い背景" in alt or "white background" in alt_l:
        flags.append("studio-shot")
    if "歩く" in alt or "歩きます" in alt or "walking" in alt_l:
        flags.append("walking")
    if "後ろ姿" in alt or "back view" in alt_l or "背中" in alt or "from behind" in alt_l:
        flags.append("back-view-strong")
    if "手元" in alt or "hands" in alt_l or "手の" in alt:
        flags.append("hands-focused")
    if "グラフ" in alt or "chart" in alt_l or "矢印" in alt or "シルエット" in alt or "silhouette" in alt_l:
        flags.append("abstract-strong")
    if "国旗" in alt or "flag" in alt_l:
        flags.append("flag")
    if "日本人" in alt or "japanese" in alt_l or "アジア" in alt or "asian" in alt_l:
        flags.append("japanese/asian-explicit")
    return flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path, help="eval json")
    ap.add_argument("--out", type=Path, help="md output")
    ap.add_argument("--limit-per-topic", type=int, default=4)
    args = ap.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))

    # group articles by topic
    by_topic: dict[str, list[dict]] = {}
    for a in data["articles"]:
        t = topic_of(a.get("title", ""))
        by_topic.setdefault(t, []).append(a)

    lines: list[str] = []
    lines.append(f"# Eval summary: {args.input.name}")
    lines.append("")

    total_slots = 0
    valid_slots = 0
    for a in data["articles"]:
        for s in a["suggestions"]:
            total_slots += 1
            if s["candidates"]:
                valid_slots += 1
    lines.append(
        f"- Total articles: **{len(data['articles'])}**, slots: **{total_slots}**, "
        f"valid (non-empty): **{valid_slots}**, empty: **{total_slots - valid_slots}**"
    )
    lines.append("")
    lines.append("- Topics: " + ", ".join(f"{t}({len(v)})" for t, v in sorted(by_topic.items(), key=lambda kv: -len(kv[1]))))
    lines.append("")
    lines.append("---")

    for topic, articles in sorted(by_topic.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"\n## Topic: {topic} ({len(articles)} articles)\n")
        for a in articles[: args.limit_per_topic]:
            title = a.get("title", "")[:80]
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"- file: `{a['file']}`")
            actual_descs = [
                f"[{img.get('role','')}/{img.get('source','')}] {img.get('alt','')[:80]}"
                for img in a.get("actual", [])
            ]
            if actual_descs:
                lines.append("- ACTUAL published images:")
                for d in actual_descs:
                    lines.append(f"  - {d}")
            for s in a["suggestions"]:
                if not s["candidates"]:
                    continue
                lines.append("")
                lines.append(
                    f"#### Slot `{s['slot_key']}` — *{s['slot_label']}* "
                    f"(type {s['type_code']}/{s.get('type_label','')}, "
                    f"query='{s['primary_query']}')"
                )
                for c in s["candidates"][:5]:
                    alt = c.get("alt", "")
                    flags = candidate_flags(alt, s["slot_label"], s["primary_query"])
                    flag_str = " ".join(f"`{f}`" for f in flags) if flags else ""
                    lines.append(f"  - {alt[:100]}  {flag_str}")
            lines.append("")
        if len(articles) > args.limit_per_topic:
            lines.append(
                f"_(omitted {len(articles) - args.limit_per_topic} more {topic} articles)_"
            )
            lines.append("")

    md = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
