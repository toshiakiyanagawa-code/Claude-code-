"""CLI for the President Online CMS entry assistant."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import click

from cms_entry_assistant.conversion_engine import ConversionConfig, convert
from cms_entry_assistant.docx_parser import parse_docx, parse_text
from cms_entry_assistant.instruction_canonical import format_canonical
from cms_entry_assistant.instruction_parser import derive_from_manuscript, parse_instruction
from cms_entry_assistant.photo_audit import build_photo_audit, write_photo_audit_html
from cms_entry_assistant.photographer_lookup import PhotographerLookup
from cms_entry_assistant.renderer import render_full_html, render_unresolved_report


@click.group()
def main() -> None:
    """President Online CMS entry assistant."""


@main.command("parse-docx")
@click.argument("docx_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Print full parsed JSON.")
def parse_docx_cmd(docx_path: Path, as_json: bool) -> None:
    """Parse a .docx or .txt manuscript and print a summary."""

    manuscript = _parse_manuscript(docx_path)
    if as_json:
        click.echo(json.dumps(asdict(manuscript), ensure_ascii=False, indent=2))
        return
    click.echo(f"source: {manuscript.source_file}")
    click.echo(f"title: {_first(manuscript.title_candidates)}")
    click.echo(f"shoulder: {_first(manuscript.shoulder_candidates)}")
    click.echo(f"lead paragraphs: {len(manuscript.lead_candidates)}")
    click.echo(f"body blocks: {len(manuscript.body_blocks)}")
    click.echo(
        "headings: "
        + str(len([block for block in manuscript.body_blocks if block.kind == "heading_h4"]))
    )
    if manuscript.caution_notes:
        click.echo("caution:")
        for note in manuscript.caution_notes:
            click.echo(f"  - {note}")
    if manuscript.author_profile:
        click.echo("author_profile: detected")
    if manuscript.parse_warnings:
        click.echo("warnings:")
        for warning in manuscript.parse_warnings:
            click.echo(f"  - {warning}")


@main.command("parse-instruction")
@click.argument("instruction_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Print full parsed JSON.")
def parse_instruction_cmd(instruction_file: Path, as_json: bool) -> None:
    """Parse a submission-instruction text file."""

    instruction = parse_instruction(instruction_file.read_text(encoding="utf-8"))
    if as_json:
        click.echo(json.dumps(asdict(instruction), ensure_ascii=False, indent=2))
        return
    click.echo(f"recipient: {instruction.recipient or '(未検出)'}")
    click.echo(f"article_type: {instruction.article_type}")
    click.echo(f"title: {instruction.title or '(未指定)'}")
    click.echo(f"shoulder: {instruction.shoulder or '(未指定)'}")
    click.echo(f"photos: {len(instruction.photo_instructions)}")
    for photo in instruction.photo_instructions:
        asset = photo.asset_id or photo.asset_url or "(id/urlなし)"
        click.echo(f"  - {photo.page_label} [{photo.source_kind}] {asset}")
    if instruction.yahoo_related_images:
        click.echo(f"yahoo_related_images: {len(instruction.yahoo_related_images)}")


@main.command("convert")
@click.option("--docx", "docx_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--instruction", "instruction_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("output/cms_entry_assistant"))
@click.option("--dict-path", type=click.Path(dir_okay=False, path_type=Path), default=Path("data/photographer_lookup.json"))
@click.option("--allow-network", is_flag=True, help="Allow iStock metadata lookups for missing photographer names.")
def convert_cmd(
    docx_path: Path,
    instruction_path: Path | None,
    out_dir: Path,
    dict_path: Path,
    allow_network: bool,
) -> None:
    """Convert a manuscript + optional instruction into CMS-ready files."""

    out_dir.mkdir(parents=True, exist_ok=True)
    manuscript = _parse_manuscript(docx_path)
    if instruction_path:
        submission = parse_instruction(instruction_path.read_text(encoding="utf-8"))
    else:
        submission = derive_from_manuscript(manuscript)

    lookup = PhotographerLookup(dict_path)
    draft = convert(
        manuscript,
        submission,
        photographer=lookup,
        config=ConversionConfig(allow_network=allow_network),
    )
    lookup.save()

    full_html = render_full_html(draft)
    report = render_unresolved_report(draft)
    canonical = format_canonical(
        submission,
        recipient=submission.recipient,
        author_profile=draft.author_profile_confirmation,
    )

    (out_dir / "draft.json").write_text(
        json.dumps(asdict(draft), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "body.html").write_text(draft.body_html_rendered, encoding="utf-8")
    (out_dir / "full.html").write_text(full_html, encoding="utf-8")
    (out_dir / "checklist.md").write_text(report, encoding="utf-8")
    (out_dir / "canonical_instruction.txt").write_text(canonical, encoding="utf-8")

    high = len([item for item in draft.unresolved_items if item.severity == "high"])
    warn = len([item for item in draft.unresolved_items if item.severity == "warn"])
    click.echo(f"wrote: {out_dir}")
    click.echo(f"unresolved: high={high} warn={warn} total={len(draft.unresolved_items)}")
    click.echo(f"html: {out_dir / 'full.html'}")
    click.echo(f"checklist: {out_dir / 'checklist.md'}")


@main.command("dict-seed")
@click.argument("cms_text_paths", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dict-path", type=click.Path(dir_okay=False, path_type=Path), default=Path("data/photographer_lookup.json"))
def dict_seed_cmd(cms_text_paths: tuple[Path, ...], dict_path: Path) -> None:
    """Seed the photographer dictionary by scanning past CMS source files."""

    lookup = PhotographerLookup(dict_path)
    count = lookup.seed_from_cms_files(cms_text_paths)
    lookup.save()
    click.echo(f"seeded {count} mappings into {dict_path}")
    click.echo(f"known photographers: {len(lookup.known_usernames())}")


@main.command("dict-add")
@click.argument("asset_id")
@click.argument("photographer_username")
@click.option("--dict-path", type=click.Path(dir_okay=False, path_type=Path), default=Path("data/photographer_lookup.json"))
@click.option("--verified", is_flag=True, help="Mark the mapping as manually verified.")
def dict_add_cmd(
    asset_id: str, photographer_username: str, dict_path: Path, verified: bool
) -> None:
    """Manually register an iStock asset_id -> photographer_username binding."""

    lookup = PhotographerLookup(dict_path)
    entry = lookup.upsert(
        asset_id,
        photographer_username,
        registered_by="cli",
        review_status="verified" if verified else "manual",
    )
    lookup.save()
    click.echo(f"registered {entry.asset_id} -> {entry.photographer_username}")


@main.command("photo-audit")
@click.option("--manuscripts-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path("data/manuscripts"), show_default=True)
@click.option("--published-path", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=Path("data/published_articles.json"), show_default=True)
@click.option("--cache-path", type=click.Path(dir_okay=False, path_type=Path), default=Path("data/istock_search_cache.json"), show_default=True)
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path), default=Path("output/cms_entry_assistant/photo_audit.html"), show_default=True)
@click.option("--limit", "article_limit", type=int, default=0, show_default=True, help="Maximum matched articles to include; 0 means all.")
@click.option("--slots", "slots_per_article", type=int, default=4, show_default=True, help="Suggestion slots per article.")
@click.option("--hits", "hits_per_slot", type=int, default=5, show_default=True, help="Cached candidate thumbnails per slot.")
@click.option("--refresh-missing", is_flag=True, help="Fetch missing iStock candidate thumbnails. Slow; uses local cache.")
@click.option("--max-refresh", type=int, default=0, show_default=True, help="Maximum cache misses to fetch when --refresh-missing is set; 0 means no cap.")
def photo_audit_cmd(
    manuscripts_dir: Path,
    published_path: Path,
    cache_path: Path,
    out_path: Path,
    article_limit: int,
    slots_per_article: int,
    hits_per_slot: int,
    refresh_missing: bool,
    max_refresh: int,
) -> None:
    """Build a static HTML report comparing suggestions with published photos."""

    report = build_photo_audit(
        manuscripts_dir,
        published_path=published_path,
        cache_path=cache_path,
        article_limit=max(0, article_limit),
        slots_per_article=max(1, slots_per_article),
        hits_per_slot=max(1, hits_per_slot),
        refresh_missing=refresh_missing,
        max_refresh=max(0, max_refresh),
    )
    written = write_photo_audit_html(report, out_path)
    click.echo(f"wrote: {written}")
    click.echo(
        "articles: "
        f"matched={report.stats.matched_articles} unmatched={report.stats.unmatched_articles} "
        f"manuscripts={report.stats.manuscripts_total}"
    )
    click.echo(
        "suggestions: "
        f"total={report.stats.suggestions_total} "
        f"cached={report.stats.suggestions_with_cached_hits} "
        f"missing={report.stats.cache_misses} "
        f"refreshed={report.stats.refreshed_queries}"
    )


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8767, show_default=True, type=int)
def serve_cmd(host: str, port: int) -> None:
    """Start the merged CMS入稿アシスト Web UI (.docx → 4-tab workflow)."""

    import uvicorn

    uvicorn.run("cms_entry_assistant.web.app:app", host=host, port=port, reload=False)


def _parse_manuscript(path: Path):
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    if path.suffix.lower() in {".txt", ".md"}:
        return parse_text(path)
    raise click.ClickException(f"unsupported manuscript format: {path.suffix}")


def _first(values: list[str]) -> str:
    return next((value for value in values if value), "")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
