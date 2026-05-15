"""Analyze the photo selection criteria from collected President Online articles.

Inputs:
  - data/manuscripts_index.json  — manuscript title + h4 + lead
  - data/published_articles.json — published version + per-page images

Output:
  - stdout report with:
      * coverage (how many manuscripts mapped to published articles)
      * per-image: position (hero/h4_n), source category, alt/caption keywords
      * source distribution (iStock / Wikimedia / 提供 / イメージ / 報道 / 不明)
      * iStock photographer frequency
      * caption "※写真はイメージです" frequency
      * heading→photo subject mapping (high-level)
      * non-trivial findings (subject preferences per topic)
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

INDEX_PATH = Path("data/manuscripts_index.json")
PUBLISHED_PATH = Path("data/published_articles.json")


# ---- Topic classifier (very rough; based on title keywords) -----------------


_TOPIC_KEYWORDS: list[tuple[str, list[str]]] = [
    ("健康・医療", ["和田秀樹", "医師", "医療", "病気", "健康", "予防", "ヨボヨボ", "寝たきり",
                    "腰痛", "骨", "がん", "癌", "脳", "認知症", "免疫", "酸化", "血管", "腸",
                    "免許返納", "アーチ", "足", "酒", "ラーメン", "ジョギング"]),
    ("食・栄養", ["野菜", "食べ方", "栄養", "食材", "サプリ", "ドレッシング", "果物", "みかん"]),
    ("歴史・戦争", ["原爆", "戦争", "降伏", "ポツダム", "中国史", "毛沢東", "習近平", "古代",
                    "古代王朝", "イラン", "歴史", "戦国", "幕末", "処刑人", "中世"]),
    ("ビジネス・マナー", ["敬語", "上司", "リーダー", "カスハラ", "クレーマー", "コミュニケーション",
                          "公務員", "市役所", "職場", "断り"]),
    ("社会", ["再開発", "タワマン", "ネパール", "インネパ", "移民", "カレー移民", "不登校",
              "教育", "学校", "子ども", "親"]),
    ("経済・キャリア", ["年収", "退職", "公務員", "年金"]),
    ("占い・銀座のママ", ["銀座のママ", "運", "占い", "人相", "性格"]),
]


def topic_of(title: str) -> str:
    """Pick the first matching topic, else '他'."""
    if not title:
        return "他"
    for topic, kws in _TOPIC_KEYWORDS:
        for kw in kws:
            if kw in title:
                return topic
    return "他"


# ---- Image classification ---------------------------------------------------


# Subject hints from alt/caption text
_SUBJECT_PATTERNS: list[tuple[str, list[str]]] = [
    ("人物・実在(報道)", ["首相", "大統領", "総書記", "国家主席", "氏", "さん", "議員",
                          "選手", "教授", "監督", "歌手"]),
    ("ポートレート・取材写真", ["インタビュー", "撮影", "提供"]),
    ("食べ物", ["カレー", "野菜", "肉", "魚", "ご飯", "果物", "みかん", "サラダ", "料理",
                "食事", "食べ物"]),
    ("身体・医療", ["腰", "足", "肩", "背中", "皮膚", "目", "歯", "脳", "細胞", "病院",
                    "医師", "薬", "聴診器", "ストレッチ", "血圧", "血管"]),
    ("高齢者・シニア", ["シニア", "高齢", "老人", "老後", "おじいさん", "おばあさん"]),
    ("お金・札束・財布", ["札束", "お金", "現金", "通帳", "財布", "貯金", "年金", "電卓"]),
    ("オフィス・ビジネスシーン", ["オフィス", "会社", "社員", "上司", "部下", "会議", "デスク",
                                  "パソコン", "ノートパソコン"]),
    ("建物・街・風景", ["タワマン", "マンション", "ビル", "街", "都市", "山", "登山",
                        "町", "国", "風景"]),
    ("子ども・学校", ["子ども", "子供", "児童", "学校", "教室", "親子"]),
    ("動物・ペット", ["犬", "猫", "ペット", "動物"]),
    ("古代史・遺跡・歴史画", ["遺跡", "土器", "甲骨", "墓", "城", "古代", "古墳", "兵士",
                              "武将", "肖像"]),
    ("文書・本・道具", ["書類", "資料", "辞書", "教科書", "ノート", "本", "雑誌"]),
]


def classify_subject(alt: str, caption: str) -> str:
    text = f"{alt} {caption}"
    for label, keys in _SUBJECT_PATTERNS:
        for kw in keys:
            if kw in text:
                return label
    return "(その他)"


# Hero detection: first figure on page 1 is conventionally the カンバン.
def annotate_positions(pages: list[dict]) -> list[dict]:
    """Flatten all images across pages with position labels.

    Uses the ``role`` field set by the fetcher (``hero`` / ``body``). The
    hero image is labeled ``カンバン``; body images get ``P{page}本文`` to
    distinguish them from the hero on multi-page articles.
    """
    out = []
    seen_src: set[str] = set()
    for p in pages:
        if "error" in p:
            continue
        page_num = p.get("page", 1)
        for img in p.get("images", []):
            src = img.get("src", "")
            if not src or src in seen_src:
                continue
            seen_src.add(src)
            role = img.get("role", "")
            if role == "hero":
                pos = "カンバン"
            else:
                pos = f"P{page_num}本文"
            out.append({
                **img,
                "position": pos,
                "page": page_num,
                "subject": classify_subject(img.get("alt", ""), img.get("caption", "")),
            })
    return out


# ---- Report -----------------------------------------------------------------


def main() -> None:
    if not PUBLISHED_PATH.exists():
        print(f"[!] missing {PUBLISHED_PATH}")
        return
    pub = json.loads(PUBLISHED_PATH.read_text(encoding="utf-8"))
    idx = {r["file"]: r for r in json.loads(INDEX_PATH.read_text(encoding="utf-8"))}

    matched, unmatched, errors = [], [], []
    for key, val in pub.items():
        if val.get("error"):
            unmatched.append((key, val))
        elif "pages" in val:
            matched.append((key, val))
        else:
            errors.append((key, val))

    print("=" * 76)
    print("プレジデントオンライン 写真選定基準 分析レポート")
    print("=" * 76)
    print(f"原稿総数: {len(idx)}")
    print(f"  - 公開記事と一致: {len(matched)}")
    print(f"  - 一致なし(未公開/書き換え): {len(unmatched)}")
    print(f"  - エラー: {len(errors)}")

    # Coverage detail
    print("\n[マッチした記事サンプル]")
    for key, art in matched[:5]:
        print(f"  - {key[:50]}")
        print(f"    -> 公開タイトル: {art.get('published_title','')[:70]}")

    print("\n[マッチしなかった原稿(上位5本)]")
    for key, val in unmatched[:5]:
        title = val.get("manuscript_title", "")
        cands = val.get("candidates", [])
        best = cands[0] if cands and "score" in cands[0] else {}
        print(f"  - {key[:50]}")
        print(f"    title: {title[:60]}")
        print(f"    best candidate: {best.get('text','')[:60]} (score={best.get('score','n/a')})")

    if not matched:
        print("\nマッチした記事が0件なので、これ以上の分析はできません。")
        return

    # ---- Per-topic + per-position image stats ----
    source_counter: Counter = Counter()
    subject_counter: Counter = Counter()
    photographer_counter: Counter = Counter()
    image_kind_counter: Counter = Counter()  # "イメージです" vs explicit subject

    by_topic_source: dict[str, Counter] = defaultdict(Counter)
    by_position_source: dict[str, Counter] = defaultdict(Counter)
    by_topic_subject: dict[str, Counter] = defaultdict(Counter)
    by_position_subject: dict[str, Counter] = defaultdict(Counter)

    # Capture some sample captions per topic/subject
    sample_captions: dict[tuple[str, str], list[str]] = defaultdict(list)

    total_articles = 0
    total_images = 0
    for key, art in matched:
        total_articles += 1
        title = art.get("manuscript_title", "")
        topic = topic_of(title)
        flat = annotate_positions(art.get("pages", []))
        for img in flat:
            total_images += 1
            src = img.get("source", "不明")
            subj = img.get("subject", "(その他)")
            pos = img.get("position", "")
            cap = img.get("caption", "") or img.get("alt", "")
            source_counter[src] += 1
            subject_counter[subj] += 1
            by_topic_source[topic][src] += 1
            by_position_source[pos][src] += 1
            by_topic_subject[topic][subj] += 1
            by_position_subject[pos][subj] += 1
            if img.get("photographer"):
                photographer_counter[img["photographer"]] += 1
            if "イメージです" in cap or "写真はイメージ" in cap:
                image_kind_counter["※写真はイメージです"] += 1
            elif img.get("source") == "iStock":
                image_kind_counter["iStock(キャプション無し)"] += 1
            else:
                image_kind_counter[src] += 1
            if len(sample_captions[(topic, subj)]) < 2 and cap:
                sample_captions[(topic, subj)].append(cap[:100])

    # ---- Output ----
    print("\n" + "=" * 76)
    print("1. 全体の写真ソース分布")
    print("=" * 76)
    print(f"  記事数: {total_articles}, 写真数: {total_images}")
    print(f"  記事あたり平均写真数: {total_images / max(1,total_articles):.1f} 枚")
    for src, n in source_counter.most_common():
        pct = 100 * n / max(1, total_images)
        print(f"    {src:20s} {n:4d} 枚 ({pct:5.1f}%)")

    print("\n[写真キャプション種別]")
    for k, n in image_kind_counter.most_common():
        print(f"    {k:30s} {n:4d}")

    print("\n" + "=" * 76)
    print("2. 被写体カテゴリ分布")
    print("=" * 76)
    for subj, n in subject_counter.most_common():
        pct = 100 * n / max(1, total_images)
        print(f"    {subj:24s} {n:4d} 枚 ({pct:5.1f}%)")

    print("\n" + "=" * 76)
    print("3. iStock 撮影者(出現回数)")
    print("=" * 76)
    for name, n in photographer_counter.most_common(20):
        print(f"    {name:30s} {n:3d} 回")
    print(f"  ユニーク撮影者数: {len(photographer_counter)}")

    print("\n" + "=" * 76)
    print("4. ジャンル(原稿タイトル)別の写真ソース")
    print("=" * 76)
    for topic, c in sorted(by_topic_source.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        print(f"  [{topic}]  写真数 {total}")
        for src, n in c.most_common():
            print(f"      {src:20s} {n:3d} ({100*n/max(1,total):.0f}%)")

    print("\n" + "=" * 76)
    print("5. ポジション(カンバン/Pn)別の写真ソース")
    print("=" * 76)
    for pos, c in sorted(by_position_source.items(), key=lambda kv: (kv[0] != "カンバン", kv[0])):
        total = sum(c.values())
        print(f"  [{pos}]  写真数 {total}")
        for src, n in c.most_common():
            print(f"      {src:20s} {n:3d} ({100*n/max(1,total):.0f}%)")

    print("\n" + "=" * 76)
    print("6. ジャンル × 被写体カテゴリ (どんなジャンルにどんな被写体)")
    print("=" * 76)
    for topic, c in sorted(by_topic_subject.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        print(f"  [{topic}]  写真数 {total}")
        for subj, n in c.most_common(8):
            print(f"      {subj:24s} {n:3d} ({100*n/max(1,total):.0f}%)")
            samples = sample_captions.get((topic, subj), [])
            for s in samples[:1]:
                print(f"          例: {s}")

    print("\n" + "=" * 76)
    print("7. ポジション × 被写体 (カンバンに来やすい被写体は?)")
    print("=" * 76)
    for pos, c in sorted(by_position_subject.items(), key=lambda kv: (kv[0] != "カンバン", kv[0])):
        total = sum(c.values())
        if total < 3:
            continue
        print(f"  [{pos}]  写真数 {total}")
        for subj, n in c.most_common(6):
            print(f"      {subj:24s} {n:3d} ({100*n/max(1,total):.0f}%)")

    # ---- Heuristics / findings ----
    print("\n" + "=" * 76)
    print("8. 自動抽出された傾向(ヒューリスティック)")
    print("=" * 76)
    findings: list[str] = []
    istock_n = source_counter.get("iStock", 0)
    if istock_n / max(1, total_images) > 0.5:
        findings.append(
            f"iStock の使用比率が高い(全{total_images}枚中{istock_n}枚={100*istock_n/total_images:.0f}%)。"
        )
    if image_kind_counter.get("※写真はイメージです", 0) / max(1, total_images) > 0.4:
        findings.append(
            "「※写真はイメージです」キャプションが多い → 抽象/イメージ系の写真選定が中心。"
        )
    # Hero hero hero
    hero_c = by_position_source.get("カンバン", Counter())
    hero_total = sum(hero_c.values())
    if hero_total and hero_c.get("iStock", 0) / hero_total > 0.6:
        findings.append(
            f"カンバンの {100*hero_c['iStock']/hero_total:.0f}% が iStock → カンバンは iStock 写真が基本。"
        )
    # Topic-specific
    for topic, c in by_topic_subject.items():
        total = sum(c.values())
        if total < 5:
            continue
        top_subj, n = c.most_common(1)[0]
        if n / total > 0.3:
            findings.append(
                f"[{topic}] では被写体 '{top_subj}' が頻出 ({n}/{total} = {100*n/total:.0f}%)。"
            )
    if not findings:
        findings.append("(明確な傾向は抽出できませんでした。サンプル数を増やしてください)")
    for f in findings:
        print(f"  - {f}")


if __name__ == "__main__":
    main()
