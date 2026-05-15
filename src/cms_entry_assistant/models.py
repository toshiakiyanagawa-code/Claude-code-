"""Shared data models for the CMS entry assistant.

The assistant turns President Online manuscript drafts and submission
instructions into CMS-ready fields, article body HTML, photo placement notes,
and an editor checklist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BodyBlockKind = Literal[
    "paragraph",
    "heading_h4",
    "heading_h5_candidate",
    "kakomi_start",
    "kakomi_end",
    "page_break",
    "credit",
    "caption",
    "editor_note",
    "blank",
    "raw",
]

ArticleType = Literal["book_excerpt", "series", "spot", "best_republish", "unknown"]
DisplayMode = Literal["source_div", "caption_inline", "none"]


@dataclass
class BodyBlock:
    kind: BodyBlockKind
    text: str = ""
    # (start, end, style) spans. style is currently "bold" or "italic".
    runs: list[tuple[int, int, str]] = field(default_factory=list)
    src_line: int = 0


@dataclass
class Manuscript:
    source_file: str
    title_candidates: list[str] = field(default_factory=list)
    subtitle_candidates: list[str] = field(default_factory=list)
    shoulder_candidates: list[str] = field(default_factory=list)
    lead_candidates: list[str] = field(default_factory=list)
    excerpt_range: str = ""
    caution_notes: list[str] = field(default_factory=list)
    body_blocks: list[BodyBlock] = field(default_factory=list)
    author_profile: str = ""
    raw_text: str = ""
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class PhotoInstruction:
    """One line in the submission instruction's photo specification."""

    page_label: str
    page_normalized: str = ""
    source_kind: str = "unknown"
    asset_id: str = ""
    asset_url: str = ""
    anchor_text: str = ""
    raw_label: str = ""
    is_image_default_caption: bool = False
    note: str = ""


@dataclass
class YahooRelatedImage:
    target_page: str
    target_asset_id: str = ""
    link_title: str = ""


@dataclass
class SubmissionInstruction:
    recipient: str = ""
    article_type: ArticleType = "unknown"
    series_name: str = ""
    book_info: str = ""
    title: str = ""
    shoulder: str = ""
    article_url: str = ""
    test_page_due: str = ""
    publish_schedule: str = ""
    photo_instructions: list[PhotoInstruction] = field(default_factory=list)
    photo_spec_raw: str = ""
    chart_count: str = ""
    category: str = ""
    external_distribution: str = ""
    yahoo_related_images: list[YahooRelatedImage] = field(default_factory=list)
    remarks: str = ""
    expansion_flags: list[str] = field(default_factory=list)
    closeup_tag: str = ""
    author_profile_instruction: str = ""
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class ImagePlacement:
    key: str
    role: str
    page_label: str = ""
    source_type: str = "unknown"
    source_label_raw: str = ""
    asset_id: str = ""
    asset_url: str = ""
    photographer_username: str = ""
    credit_text: str = ""
    caption: str = ""
    alt: str = ""
    anchor_text: str = ""
    display_mode: DisplayMode = "source_div"
    html_placeholder: str = ""
    yahoo_related_title: str = ""
    width_px: int = 670
    figure_orient: str = "figure-center"
    unresolved_fields: list[str] = field(default_factory=list)


@dataclass
class IstockSearchSuggestion:
    """A search query suggestion for a photo slot."""

    slot_key: str
    slot_label: str
    type_code: str = "C"
    type_label: str = ""
    query_ja: str = ""
    query_en: str = ""
    search_url_ja: str = ""
    search_url_en: str = ""
    query_plan: list[str] = field(default_factory=list)
    rationale: str = ""
    picked_asset_id: str = ""
    note: str = ""
    # CMS のページ番号 (1=カンバン+最初の h4 群, 2=(2ページ目) マーカー後, ...)。
    # 原稿に (Nページ目) マーカーがない / build_suggestion 直呼び時は 0。
    page_number: int = 0


@dataclass
class UnresolvedItem:
    code: str
    severity: str = "info"
    message: str = ""
    source: str = ""
    target_field: str = ""
    suggested_action: str = ""
    raw_text: str = ""


@dataclass
class CMSDraft:
    selected_title: str = ""
    selected_subtitle: str = ""
    selected_shoulder: str = ""
    selected_lead: str = ""
    book_attribution_html: str = ""
    body_html: str = ""
    body_html_rendered: str = ""
    meta_fields: dict = field(default_factory=dict)
    image_placements: list[ImagePlacement] = field(default_factory=list)
    photo_suggestions: list[IstockSearchSuggestion] = field(default_factory=list)
    yahoo_related_images: list[YahooRelatedImage] = field(default_factory=list)
    category: str = ""
    external_distribution: str = ""
    expansion_flags: list[str] = field(default_factory=list)
    author_profile_confirmation: str = ""
    closeup_tag: str = ""
    unresolved_items: list[UnresolvedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PhotographerEntry:
    asset_id: str
    photographer_username: str
    source_url: str = ""
    first_seen_at: str = ""
    last_used_at: str = ""
    usage_count: int = 0
    note: str = ""
    registered_by: str = ""
    review_status: str = "auto"
