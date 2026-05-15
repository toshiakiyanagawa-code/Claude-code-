"""CMS 写真候補パイプライン評価スクリプト.

data/manuscripts/ の docx を 1 件ずつ現在の web パイプライン (LLM + Policy-3)
に通し、各 slot の上位候補と実際の公開写真 (data/published_articles.json) を
並べた JSON レポートを出力する。

Usage:
  uv run python scripts/cms_photo_eval.py --limit 5 > output/eval_v1.json

LLM / iStock cache は使う (CMS_ENTRY_ASSISTANT_DISABLE_LLM_CACHE=1 で無効化可)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_docx, parse_text
from cms_entry_assistant.instruction_parser import derive_from_manuscript
from cms_entry_assistant.web.app import _fetch_candidates


def _parse(path: Path):
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    return parse_text(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manuscripts-dir", type=Path, default=Path("data/manuscripts"))
    ap.add_argument(
        "--published-path", type=Path, default=Path("data/published_articles.json")
    )
    ap.add_argument("--limit", type=int, default=5, help="manuscripts to evaluate")
    ap.add_argument(
        "--max-slots", type=int, default=4, help="cap suggestions per article"
    )
    ap.add_argument("--search-mode", default="llm_rerank")
    ap.add_argument(
        "--out", type=Path, default=None, help="write JSON here (default stdout)"
    )
    args = ap.parse_args()

    published = json.loads(args.published_path.read_text(encoding="utf-8"))
    paths = sorted(args.manuscripts_dir.glob("*.docx"))
    matched = [p for p in paths if p.name in published and "pages" in published[p.name]]
    todo = matched[: args.limit]

    print(
        f"[eval] manuscripts={len(paths)} matched={len(matched)} todo={len(todo)}",
        file=sys.stderr,
    )

    out: dict = {"articles": [], "config": {"search_mode": args.search_mode}}

    for idx, path in enumerate(todo, start=1):
        t0 = time.time()
        record = published[path.name]
        manuscript = _parse(path)
        submission = derive_from_manuscript(manuscript)
        draft = convert(
            manuscript, submission, config=ConversionConfig(allow_network=False)
        )
        article_title = (
            getattr(draft, "selected_title", "")
            or (manuscript.title_candidates[0] if manuscript.title_candidates else "")
            or submission.title
            or ""
        )

        suggestions_capped = list(draft.photo_suggestions[: args.max_slots])
        candidates = _fetch_candidates(
            suggestions_capped,
            search_mode=args.search_mode,
            article_title=article_title,
        )

        suggestion_blocks = []
        for s in suggestions_capped:
            hits = candidates.get(s.slot_key, [])[:5]
            suggestion_blocks.append(
                {
                    "slot_key": s.slot_key,
                    "slot_label": s.slot_label,
                    "type_code": s.type_code,
                    "type_label": s.type_label,
                    "primary_query": s.query_ja,
                    "candidates": [
                        {
                            "asset_id": h.asset_id,
                            "alt": h.alt[:200] if h.alt else "",
                            "photographer": h.photographer_username,
                        }
                        for h in hits
                    ],
                }
            )

        actual_imgs = []
        for page in record.get("pages", []):
            for img in page.get("images", []):
                actual_imgs.append(
                    {
                        "role": img.get("role", ""),
                        "alt": (img.get("alt") or "")[:200],
                        "source": img.get("source", ""),
                        "photographer": img.get("photographer", ""),
                    }
                )

        article = {
            "file": path.name,
            "title": draft.selected_title or record.get("manuscript_title", ""),
            "url": record.get("url", ""),
            "elapsed_s": round(time.time() - t0, 1),
            "suggestions": suggestion_blocks,
            "actual": actual_imgs,
        }
        out["articles"].append(article)
        print(
            f"[eval] [{idx}/{len(todo)}] {path.name} ({article['elapsed_s']}s, "
            f"slots={len(suggestion_blocks)}, actual={len(actual_imgs)})",
            file=sys.stderr,
        )

    payload = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
        print(f"[eval] wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
