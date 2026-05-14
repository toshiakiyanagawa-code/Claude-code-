"""Lightweight President Online category prediction.

This is intentionally rule-based. The generated category is a draft value for
the editor checklist, not an automatic publishing decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ライフ＞健康": (
        "健康",
        "栄養",
        "食事",
        "ダイエット",
        "肥満",
        "血糖",
        "血圧",
        "コレステロール",
        "免疫",
        "腸内",
        "野菜",
        "果物",
        "サプリ",
        "運動不足",
        "生活習慣",
    ),
    "ライフ＞医療": (
        "医療",
        "病院",
        "医師",
        "医者",
        "薬",
        "処方",
        "治療",
        "がん",
        "癌",
        "糖尿病",
        "心不全",
        "脳卒中",
        "認知症",
        "アルツハイマー",
        "手術",
        "診察",
    ),
    "ライフ＞メンタル": (
        "メンタル",
        "うつ",
        "鬱",
        "うつ病",
        "ストレス",
        "不安",
        "心理",
        "精神",
        "悩み",
        "孤独",
        "燃え尽き",
        "セルフケア",
    ),
    "ライフ＞教育": (
        "教育",
        "学校",
        "教師",
        "塾",
        "授業",
        "勉強",
        "受験",
        "保育園",
        "幼稚園",
        "子育て",
        "PTA",
        "学習",
        "宿題",
        "親子",
    ),
    "ライフ＞老後": (
        "老後",
        "定年",
        "年金",
        "シニア",
        "高齢者",
        "老人",
        "終活",
        "看取り",
        "介護",
        "相続",
    ),
    "マネー＞マーケット": (
        "株",
        "投資",
        "株式",
        "上場",
        "決算",
        "東証",
        "為替",
        "ドル",
        "円安",
        "円高",
        "金利",
        "債券",
        "M&A",
        "ファンド",
    ),
    "マネー＞介護": (
        "介護",
        "在宅介護",
        "介護保険",
        "介護施設",
        "ケアマネ",
        "高額療養費",
        "介護費",
        "看取り",
    ),
    "マネー＞不動産": (
        "不動産",
        "住宅",
        "賃貸",
        "持ち家",
        "マンション",
        "戸建て",
        "ローン",
        "リフォーム",
        "中古住宅",
        "再開発",
        "タワマン",
    ),
    "政治・経済＞国際経済": (
        "貿易",
        "関税",
        "WTO",
        "サプライチェーン",
        "GDP",
        "成長戦略",
        "中央銀行",
        "ドル高",
        "新興国",
        "国際経済",
    ),
    "政治・経済＞国際問題": (
        "外交",
        "首脳",
        "条約",
        "制裁",
        "戦争",
        "停戦",
        "封鎖",
        "米中",
        "イラン",
        "イスラエル",
        "ロシア",
        "ウクライナ",
        "核",
        "国連",
    ),
    "政治・経済＞国内政治": (
        "首相",
        "総理",
        "内閣",
        "政権",
        "与党",
        "野党",
        "選挙",
        "国会",
        "衆議院",
        "参議院",
        "自民党",
        "公明党",
        "立憲",
        "維新",
        "国民民主",
        "憲法改正",
    ),
    "キャリア＞働き方": (
        "転職",
        "退職",
        "正社員",
        "非正規",
        "派遣",
        "副業",
        "在宅勤務",
        "リモート",
        "残業",
        "サラリーマン",
        "OL",
        "上司",
        "部下",
        "出世",
        "カスハラ",
        "クレーム",
    ),
    "キャリア＞スキル": (
        "英語",
        "TOEIC",
        "資格",
        "プログラミング",
        "AI活用",
        "スキルアップ",
        "学び直し",
        "リスキリング",
        "コミュニケーション術",
        "話し方",
        "敬語",
    ),
    "社会＞皇室": (
        "皇室",
        "天皇",
        "皇后",
        "皇太子",
        "雅子",
        "愛子",
        "悠仁",
        "宮家",
        "皇位継承",
        "皇族",
    ),
    "社会＞事件": (
        "事件",
        "逮捕",
        "起訴",
        "刑事",
        "犯罪",
        "詐欺",
        "殺人",
        "強盗",
        "盗聴",
        "横領",
        "背任",
    ),
    "社会＞日本史": (
        "戦国",
        "幕末",
        "江戸時代",
        "明治維新",
        "昭和",
        "大正",
        "古代",
        "中世",
        "近代",
        "歴史的",
        "武将",
        "天下統一",
        "中国史",
        "処刑人",
    ),
    "社会＞地方": (
        "地方都市",
        "過疎",
        "限界集落",
        "地方自治体",
        "シャッター街",
        "商店街",
        "廃墟",
        "地方創生",
        "市町村合併",
    ),
    "社会＞生活トラブル": (
        "クレーム",
        "カスハラ",
        "近隣トラブル",
        "騒音",
        "ハラスメント",
        "ご近所",
        "悪質",
    ),
    "社会＞教育問題": (
        "教育制度",
        "不登校",
        "いじめ",
        "学級崩壊",
        "学力低下",
        "教員不足",
        "デジタル教科書",
        "体力テスト",
    ),
    "ビジネス＞メーカー": (
        "トヨタ",
        "ホンダ",
        "日産",
        "ソニー",
        "パナソニック",
        "シャープ",
        "日立",
        "東芝",
        "三菱",
        "メーカー",
        "製造業",
        "工場",
    ),
    "ビジネス＞IT": (
        "Google",
        "Apple",
        "Microsoft",
        "Amazon",
        "OpenAI",
        "ChatGPT",
        "AI",
        "クラウド",
        "サイバー",
        "半導体",
        "生成AI",
    ),
    "教養・雑学": (
        "歴史的経緯",
        "豆知識",
        "雑学",
        "教養",
        "由来",
        "起源",
        "アニメ",
        "ドラマ",
        "映画",
        "小説",
    ),
}

WEIGHT_TITLE = 5
WEIGHT_LEAD = 3
WEIGHT_BODY = 1
MIN_SCORE = 5
MIN_MARGIN = 3
BODY_HEAD_CHARS = 1500


@dataclass
class CategoryPrediction:
    category: str
    score: int = 0
    runner_up: str = ""
    runner_up_score: int = 0


def _count_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(text.count(keyword) for keyword in keywords if keyword)


def predict_category(manuscript: Any) -> CategoryPrediction:
    """Predict a draft CMS category from parsed manuscript fields."""

    title = " ".join(getattr(manuscript, "title_candidates", []) or [])
    lead = " ".join(getattr(manuscript, "lead_candidates", []) or [])
    body_chunks: list[str] = []
    for block in getattr(manuscript, "body_blocks", []) or []:
        text = getattr(block, "text", "")
        if isinstance(text, str) and text.strip():
            body_chunks.append(text.strip())
        if len(" ".join(body_chunks)) >= BODY_HEAD_CHARS:
            break
    body = " ".join(body_chunks)[:BODY_HEAD_CHARS]

    scored: list[tuple[int, str]] = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = (
            WEIGHT_TITLE * _count_hits(title, keywords)
            + WEIGHT_LEAD * _count_hits(lead, keywords)
            + WEIGHT_BODY * _count_hits(body, keywords)
        )
        scored.append((score, category))
    scored.sort(key=lambda item: (-item[0], item[1]))

    best_score, best_category = scored[0]
    runner_score, runner_category = scored[1] if len(scored) > 1 else (0, "")
    if best_score < MIN_SCORE or best_score - runner_score < MIN_MARGIN:
        return CategoryPrediction(
            category="",
            score=best_score,
            runner_up=runner_category,
            runner_up_score=runner_score,
        )
    return CategoryPrediction(
        category=best_category,
        score=best_score,
        runner_up=runner_category,
        runner_up_score=runner_score,
    )
