"""Render CMS draft artifacts."""

from __future__ import annotations

import html

from cms_entry_assistant.models import CMSDraft


def render_full_html(draft: CMSDraft) -> str:
    parts: list[str] = []
    if draft.selected_lead:
        lead_html = "<br>\n".join(html.escape(line) for line in draft.selected_lead.splitlines())
        parts.append(f'<div class="article-lead">{lead_html}</div>')
    if draft.book_attribution_html:
        parts.append(draft.book_attribution_html)
    if draft.body_html_rendered:
        parts.append(draft.body_html_rendered)
    return "\n\n".join(parts).strip()


def render_unresolved_report(draft: CMSDraft) -> str:
    lines: list[str] = ["# CMS入稿 確認リスト", ""]

    lines.extend(
        [
            "## メタ情報",
            f"- タイトル(採用案): {draft.selected_title or '(未抽出)'}",
            f"- ショルダー: {draft.selected_shoulder or '(未抽出)'}",
            f"- サブタイトル: {draft.selected_subtitle or '(未抽出)'}",
            f"- カテゴリ: {draft.category or '(未確定)'}",
            f"- 外部配信: {draft.external_distribution or '(未指定)'}",
        ]
    )
    if draft.expansion_flags:
        lines.append(f"- 横展開フラグ: {' / '.join(draft.expansion_flags)}")
    if draft.closeup_tag:
        lines.append(f"- Close-Up タグ: {draft.closeup_tag}")
    if draft.author_profile_confirmation:
        lines.append("- 著者プロフィール: 要確認")

    lines.extend(["", "## 写真指定"])
    if draft.image_placements:
        for placement in draft.image_placements:
            detail = f"[{placement.source_type}, {placement.display_mode}]"
            if placement.asset_id:
                detail += f" asset_id={placement.asset_id}"
            lines.append(f"- **{placement.page_label or placement.key}** {detail}")
            if placement.anchor_text:
                lines.append(f"  - 挿入位置: {placement.anchor_text} の下")
            if placement.caption:
                lines.append(f"  - キャプション: {placement.caption}")
            if placement.alt:
                lines.append(f"  - alt(暫定): {placement.alt}")
            if placement.unresolved_fields:
                lines.append(f"  - 未解決: {', '.join(placement.unresolved_fields)}")
    else:
        lines.append("- (本文中の写真マーカーなし)")

    if draft.yahoo_related_images:
        lines.extend(["", "## Yahoo!転載用画像"])
        for item in draft.yahoo_related_images:
            target = item.target_page
            if item.target_asset_id:
                target = f"{target} / asset_id={item.target_asset_id}"
            lines.append(f"- {target}: {item.link_title}")

    if draft.photo_suggestions:
        lines.extend(["", "## 各小見出しのiStock写真候補"])
        for suggestion in draft.photo_suggestions:
            lines.extend(
                [
                    "",
                    f"### {suggestion.slot_label} ({suggestion.slot_key}) — "
                    f"[{suggestion.type_code}] {suggestion.type_label}",
                    f"- クエリ(JP): `{suggestion.query_ja}`",
                    f"- クエリ(EN): `{suggestion.query_en}`",
                    f"- iStock日本: {suggestion.search_url_ja}",
                    f"- iStock英語: {suggestion.search_url_en}",
                    f"- 判定理由: {suggestion.rationale}",
                ]
            )
            if suggestion.note:
                lines.append(f"- 選定メモ: {suggestion.note}")

    if draft.warnings:
        lines.extend(["", "## パース警告"])
        for warning in draft.warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "## 編集者の確認が必要な項目"])
    if not draft.unresolved_items:
        lines.append("- なし")
    else:
        severity_order = {"high": 0, "warn": 1, "info": 2}
        items = sorted(
            draft.unresolved_items,
            key=lambda item: (severity_order.get(item.severity, 9), item.code),
        )
        current = ""
        for item in items:
            severity = item.severity.upper()
            if severity != current:
                current = severity
                lines.extend(["", f"### {severity}"])
            lines.append(f"- [{item.code}] {item.message}")
            if item.suggested_action:
                lines.append(f"  - 対応: {item.suggested_action}")
            if item.raw_text:
                lines.append(f"  - 該当: `{item.raw_text}`")

    return "\n".join(lines).rstrip() + "\n"
