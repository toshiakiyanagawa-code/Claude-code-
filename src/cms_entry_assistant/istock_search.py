"""Build iStock search queries (and URLs) for each subheading/hero position.

Restored from the earlier "clean" version (v8) per editor feedback that the
overly-aggressive _specific_subject_rule / mechanical "顔なし 後ろ姿 手元"
suffixing was making all slots converge on the same candidates.

Design (codex-reviewed 2026-05-14):
  - 5 type system only: A=ランドマーク / B=国旗+外交 / C=抽象シンボル / D=行動シーン / E=産業
  - Slot context:
      hero: lead_text (article-wide). Fallback to h4_text if no lead.
      h4_*: h4_text + first 2 surrounding paragraphs (slot-local context).
  - No mechanical suffix append (人種/構図サフィックスはここでは付けない).
  - Up to 3 keywords per query.

We do NOT scrape iStock search results — we generate the search URL so the
editor can click through and pick a photo in iStock's UI. The picked asset_id
is then bound to the slot via the photo-selection UI.
"""

from __future__ import annotations

import re
import urllib.parse

from cms_entry_assistant.models import IstockSearchSuggestion

# --- Lightweight Japanese keyword extraction --------------------------------

# Place names / institutions — Type A (Landmark)
LANDMARKS: dict[str, tuple[str, str]] = {
    "国会議事堂": ("Tokyo Diet Building", "国会議事堂"),
    "皇居": ("Imperial Palace Tokyo", "皇居"),
    "裁判所": ("courtroom Japan", "裁判所"),
    "検察庁": ("prosecutor office", "検察庁"),
    "警察": ("police Japan", "警察"),
    "学校": ("classroom Japan", "学校"),
    "教室": ("classroom", "教室"),
    "市役所": ("city hall Japan", "市役所"),
    "病院": ("hospital", "病院"),
    "工場": ("factory Japan", "工場"),
    "オフィス": ("office Japan", "オフィス"),
    "図書館": ("library Japan", "図書館"),
    "スーパー": ("supermarket Japan", "スーパー"),
    "コンビニ": ("convenience store Japan", "コンビニ"),
    "ショッピングセンター": ("shopping mall", "ショッピングセンター"),
}

# Countries — Type B (Flags + diplomacy)
COUNTRY_KEYWORDS: dict[str, str] = {
    "日本": "Japan",
    "中国": "China",
    "アメリカ": "USA",
    "米国": "USA",
    "韓国": "Korea",
    "ロシア": "Russia",
    "ウクライナ": "Ukraine",
    "イラン": "Iran",
    "イラク": "Iraq",
    "イスラエル": "Israel",
    "パレスチナ": "Palestine",
    "レバノン": "Lebanon",
    "シリア": "Syria",
    "サウジアラビア": "Saudi Arabia",
    "インド": "India",
    "ドイツ": "Germany",
    "フランス": "France",
    "イギリス": "UK",
    "英": "UK",
    "EU": "EU",
    "台湾": "Taiwan",
    "北朝鮮": "North Korea",
}

DIPLOMACY_WORDS: tuple[str, ...] = (
    "外交", "同盟", "対立", "制裁", "戦争", "停戦", "封鎖", "条約", "首脳",
    "輸出規制", "関税", "国交", "侵攻", "緊張", "国際", "海外",
)

# Industries — Type E
INDUSTRIES: dict[str, tuple[str, str]] = {
    "石油": ("oil rig", "石油"),
    "ガス": ("LNG plant", "ガス"),
    "電力": ("power plant", "電力"),
    "農業": ("agriculture Japan", "農業"),
    "物流": ("logistics warehouse", "物流"),
    "建設": ("construction site Japan", "建設"),
    "半導体": ("semiconductor factory", "半導体"),
    "自動車": ("automotive factory", "自動車"),
    "鉄道": ("Japanese train", "鉄道"),
    "金融": ("financial district Tokyo", "金融"),
}

# Abstract concept stand-ins — Type C
ABSTRACT: dict[str, tuple[str, str]] = {
    "AI": ("AI artificial intelligence concept", "AI"),
    "生成AI": ("generative AI concept", "生成AI"),
    "結婚": ("wedding rings", "結婚"),
    "離婚": ("divorce paperwork", "離婚"),
    "婚活": ("dating app smartphone", "婚活"),
    "孤独": ("lonely silhouette", "孤独"),
    "健康": ("healthy food", "健康"),
    "病気": ("medical consultation", "病気"),
    "睡眠": ("sleeping bedroom", "睡眠"),
    "ストレス": ("stressed worker", "ストレス"),
    "教育": ("classroom education Japan", "教育"),
    "信頼": ("handshake business", "信頼"),
    "リーダー": ("leader silhouette", "リーダー"),
    "学歴": ("university diploma", "学歴"),
    "判決": ("scales of justice", "判決"),
    "捜査": ("detective evidence", "捜査"),
    "犯罪": ("crime scene tape", "犯罪"),
    "経済": ("Tokyo stock board", "経済"),
    "投資": ("stock chart smartphone", "投資"),
    "貧困": ("empty wallet", "貧困"),
    "高齢者": ("elderly couple Japan", "高齢者"),
    "子育て": ("parent child Japan", "子育て"),
    "老後": ("retirement elderly", "老後"),
    "認知症": ("dementia elderly hands", "認知症"),
    "皇室": ("imperial palace Japan", "皇室"),
    "天皇": ("imperial palace Japan", "天皇"),
}

# Action verbs / motifs — Type D (action scenes)
ACTIONS: dict[str, tuple[str, str]] = {
    "握手": ("handshake business", "握手"),
    "演説": ("woman giving speech podium", "演説"),
    "会議": ("business meeting", "会議"),
    "対話": ("two people talking bench", "対話"),
    "面接": ("job interview Japan", "面接"),
    "デート": ("couple cafe date", "デート"),
    "通勤": ("commuter Tokyo", "通勤"),
    "勉強": ("student studying desk", "勉強"),
    "料理": ("cooking kitchen Japan", "料理"),
    "運動": ("jogging morning park", "運動"),
}


TYPE_LABEL: dict[str, str] = {
    "A": "ランドマーク",
    "B": "国旗",
    "C": "抽象シンボル",
    "D": "行動シーン",
    "E": "産業",
}


def _detect_type(text: str) -> tuple[str, list[str], list[str], str]:
    """Decide a type for the given context text.

    Returns (type_code, ja_keywords, en_keywords, rationale).

    Priority order (v8, intentionally conservative):
      A: landmark / institution
      B: ≥2 countries + a diplomacy word
      E: industry
      D: action verb
      C: abstract concept
      (fallback): generic noun extraction → type C
    """
    text = text or ""

    # Type A: landmark / institution
    matches_a = [(k, v) for k, v in LANDMARKS.items() if k in text]
    if matches_a:
        ja_kw = [k for k, _ in matches_a[:2]]
        en_kw = [v[0] for _, v in matches_a[:2]]
        ja_label = "/".join(ja_kw)
        return "A", ja_kw, en_kw, f"ランドマーク語彙『{ja_label}』を検出"

    # Type B: ≥2 countries AND a diplomacy word
    countries = [k for k in COUNTRY_KEYWORDS if k in text]
    has_diplomacy = any(w in text for w in DIPLOMACY_WORDS)
    if len(countries) >= 2 and has_diplomacy:
        ja_kw = countries[:2] + ["国旗"]
        en_kw = [" ".join(COUNTRY_KEYWORDS[c] for c in countries[:2]) + " flags"]
        return "B", ja_kw, en_kw, (
            f"複数国({'/'.join(countries[:2])}) + 外交語彙を検出"
        )

    # Type E: industry
    matches_e = [(k, v) for k, v in INDUSTRIES.items() if k in text]
    if matches_e:
        ja_kw = [k for k, _ in matches_e[:2]]
        en_kw = [v[0] for _, v in matches_e[:2]]
        return "E", ja_kw, en_kw, (
            f"産業語彙『{'/'.join(ja_kw)}』を検出"
        )

    # Type D: action verb
    matches_d = [(k, v) for k, v in ACTIONS.items() if k in text]
    if matches_d:
        ja_kw = [k for k, _ in matches_d[:2]]
        en_kw = [v[0] for _, v in matches_d[:2]]
        return "D", ja_kw, en_kw, (
            f"行動語彙『{'/'.join(ja_kw)}』を検出"
        )

    # Type C: abstract concept
    matches_c = [(k, v) for k, v in ABSTRACT.items() if k in text]
    if matches_c:
        ja_kw = [k for k, _ in matches_c[:2]]
        en_kw = [v[0] for _, v in matches_c[:2]]
        return "C", ja_kw, en_kw, (
            f"抽象概念『{'/'.join(ja_kw)}』を検出"
        )

    # Fallback: extract noun chunks from the text
    return (
        "C",
        _generic_keywords(text),
        [],
        "明示的なテーマ語が見つからなかったため、本文からキーワードを抽出",
    )


def _generic_keywords(text: str) -> list[str]:
    """Last-ditch keyword fallback: grab a few short noun-like substrings."""
    chunks = re.findall(r"[一-鿿ァ-ヿ]{2,8}", text or "")
    seen: list[str] = []
    for c in chunks:
        if c not in seen:
            seen.append(c)
        if len(seen) >= 3:
            break
    return seen


def build_suggestion(
    slot_key: str,
    slot_label: str,
    h4_text: str,
    surrounding_paragraphs: list[str] | None = None,
    lead_text: str = "",
) -> IstockSearchSuggestion:
    """Compose context, decide type, and build a suggestion object (v8 style).

    Slot context (codex-reviewed B改 + v8 restoration):
      - hero: lead_text. Fallback to h4_text if no lead.
        → The hero is the article's representative image.
      - h4_*: h4_text + first 2 surrounding paragraphs (slot-local context).
        → article-wide title is NOT used here, to avoid title-word pollution
          across all subheadings.

    No mechanical "日本人 顔なし 後ろ姿 手元" suffix is appended; the v2-era
    `_specific_subject_rule` + `_with_japanese_no_face_policy` were removed
    because they collapsed candidates from different sections onto the same
    image search.
    """
    surrounding_paragraphs = surrounding_paragraphs or []
    if slot_key == "hero":
        context = lead_text or h4_text
    else:
        context = (h4_text or "") + "\n" + "\n".join(surrounding_paragraphs[:2])

    type_code, ja_words, en_words, rationale = _detect_type(context)
    query_ja = " ".join(ja_words[:3]).strip() or (h4_text or "")[:20]
    query_en = " ".join(w for w in en_words[:3] if w).strip() or query_ja

    return IstockSearchSuggestion(
        slot_key=slot_key,
        slot_label=slot_label,
        type_code=type_code,
        type_label=TYPE_LABEL.get(type_code, "未分類"),
        query_ja=query_ja,
        query_en=query_en,
        search_url_ja=_istock_search_url(query_ja, jp=True),
        search_url_en=_istock_search_url(query_en, jp=False),
        rationale=rationale,
    )


def _istock_search_url(query: str, *, jp: bool) -> str:
    """Build the iStock public search URL (no scraping involved)."""
    encoded = urllib.parse.quote(query or "")
    if jp:
        return (
            f"https://www.istockphoto.com/jp/search/2/image?phrase={encoded}"
            "&assetfiletype=image&excludenudity=true"
        )
    return f"https://www.istockphoto.com/search/2/image?phrase={encoded}"
