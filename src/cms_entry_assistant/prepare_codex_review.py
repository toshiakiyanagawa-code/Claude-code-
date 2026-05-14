"""Prepare a structured sample for codex review of judgment criteria.

Output: data/codex_review_sample.md — a human-readable doc with:
  - 編集者が実際に選んだ alt + caption + source
  - 現行ルールが生成した type_code + クエリ + Top 3 候補 alt
  - 各スロットを行ごとに並べる

Run:
    uv run python -m cms_entry_assistant.prepare_codex_review
"""

from __future__ import annotations

import json
from pathlib import Path

INPUT_PATH = Path("output/cms_entry_assistant/diagnose_misses.json")
OUT_PATH = Path("data/codex_review_sample.md")


def _clean_alt(s: str) -> str:
    s = s or ""
    # iStock candidate alt format: '<subject> - <query> ストックフォトと画像'
    if "- " in s and s.endswith("ストックフォトと画像"):
        s = s.split(" - ")[0]
    return s.strip()


def main() -> None:
    rows = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    # Drop empty actual_alts (e.g. body slot but article had no body image)
    rows = [r for r in rows if r.get("actual_alt") and not r.get("error")]
    # Group by file → show hero + first 2 body slots per article
    by_file: dict[str, list[dict]] = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(r)
    lines: list[str] = []
    lines.append("# 候補判定基準レビュー用サンプル\n")
    lines.append(f"## 集計: {len(by_file)} 記事 / {len(rows)} スロット\n")
    # Stats: source distribution
    src_counter: dict[str, int] = {}
    type_counter: dict[str, int] = {}
    for r in rows:
        src_counter[r.get("actual_source", "?")] = src_counter.get(r.get("actual_source", "?"), 0) + 1
        type_counter[r.get("type_code", "?")] = type_counter.get(r.get("type_code", "?"), 0) + 1
    lines.append("### 実際のソース分布")
    for k, v in sorted(src_counter.items(), key=lambda x: -x[1]):
        lines.append(f"- {k}: {v}")
    lines.append("\n### 現行ルールが付けた type_code 分布")
    for k, v in sorted(type_counter.items(), key=lambda x: -x[1]):
        lines.append(f"- type_code={k}: {v}")
    lines.append("\n---\n")
    # Per-article view (limit to 50 articles to keep the doc tractable)
    for i, (file, slots) in enumerate(sorted(by_file.items())):
        if i >= 50:
            break
        lines.append(f"## [{i+1:02d}] {file}")
        title = slots[0].get("manuscript_title", "")
        lines.append(f"記事タイトル: {title}\n")
        for s in slots[:3]:
            lines.append(f"### スロット {s['slot_key']} ({s.get('slot_label','')})")
            lines.append(f"- **実採用 alt**: `{s['actual_alt']}`")
            if s.get("actual_caption"):
                lines.append(f"- 実採用 caption: {s['actual_caption']}")
            lines.append(f"- 実採用ソース: **{s.get('actual_source','?')}**")
            lines.append(f"- 現行 type_code: **{s.get('type_code','?')}**")
            lines.append(f"- 現行クエリ(ja): `{s.get('suggested_query_ja','')}`")
            lines.append(f"- 現行ルール根拠: {s.get('rationale','')}")
            top = s.get("top_candidate_alts", [])[:3]
            if top:
                lines.append("- 候補 Top3 alt:")
                for j, a in enumerate(top):
                    lines.append(f"  {j+1}. `{_clean_alt(a)}`")
            else:
                lines.append("- 候補: (キャッシュなし)")
            lines.append(f"- Jaccard(参考): {s.get('best_jaccard',0)}")
            lines.append("")
        lines.append("")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({sum(1 for _ in lines)} lines)")


if __name__ == "__main__":
    main()
