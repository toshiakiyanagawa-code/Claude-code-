"""Photographer preference dictionary + recent-use history.

This is the small "style guide" the tool uses to rank iStock search hits.
We don't have past asset_ids (they're not exposed in CMS HTML), but we do know
which photographer usernames were used in past President Online articles. We
use this as a soft preference: when the crawler returns 6-8 hits, hits by a
preferred photographer rise to the top.

We also track recent-use to discourage immediate repeats (per editor directive
2026-05-13 "短期間の同一写真再利用を避ける").

State files (JSON):
  data/photo_preferences.json  — photographer username -> usage_count, last_seen
  data/photo_usage_history.json — asset_id -> last_used_at, slot_label, h4
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PREFS_PATH = Path("data/photo_preferences.json")
DEFAULT_HISTORY_PATH = Path("data/photo_usage_history.json")

# Seed: photographer usernames observed in past President Online CMS articles.
# Source: original CMS source sample plus 40 matched manuscript/published-article
# pairs in data/published_articles.json.
# Adding to this list is intended — the dictionary grows as the editor uses the tool.
SEED_PHOTOGRAPHERS: tuple[str, ...] = (
    "78image", "Zoey106", "mapo", "fadfebrian", "kanzilyou", "vejaa",
    "KamiPhotos", "sankai", "Jinda Noipho", "Yusuke Ide", "Pressmaster",
    "licsiren", "ShutterOK", "JMrocek", "yanguolin", "JohnnyGreig", "y-studio",
    "kazuma", "byryo", "takasuu", "koumaru", "kuppa_rock", "The-Tor",
    "kawamura_lucy", "Yaraslau", "kimberrywood", "bugking88", "maruco", "key05",
    "west", "Prostock-Studio", "AJ_Watt", "Lex_16", "MARIIA", "alexis84",
    "masamasa3", "bymuratdeniz", "simonkr", "Chihiro", "bee32", "hxdbzxy",
    "Gajus", "MEDITERRANEAN",
)

SEED_PHOTOGRAPHER_WEIGHTS: dict[str, int] = {
    "kazuma": 6,
    "byryo": 4,
    "takasuu": 4,
    "koumaru": 4,
    "kuppa_rock": 4,
}

# Avoid suggesting the same asset within this many days.
RECENT_USE_WINDOW_DAYS = 60


# ---- Photographer preferences ---------------------------------------------


@dataclass
class PhotographerStats:
    username: str
    usage_count: int = 0
    last_seen: str = ""  # ISO date

    def to_dict(self) -> dict:
        return asdict(self)


class PreferencesStore:
    def __init__(self, path: Path | str = DEFAULT_PREFS_PATH):
        self.path = Path(path)
        self.stats: dict[str, PhotographerStats] = {}
        self._load_or_seed()

    def _load_or_seed(self) -> None:
        loaded_existing = False
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.stats = {
                    name: PhotographerStats(**v) for name, v in raw.items() if isinstance(v, dict)
                }
                loaded_existing = True
            except Exception:
                pass

        # Seed or merge with the known-good photographer list. Merge matters when
        # the repo ships a data/photo_preferences.json created before new article
        # samples were analyzed.
        changed = False
        for name in SEED_PHOTOGRAPHERS:
            count = SEED_PHOTOGRAPHER_WEIGHTS.get(name, 1)
            entry = self.stats.get(name)
            if entry is None:
                self.stats[name] = PhotographerStats(username=name, usage_count=count)
                changed = True
            elif entry.usage_count < count:
                entry.usage_count = count
                changed = True
        if changed or not loaded_existing:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: v.to_dict() for k, v in self.stats.items()}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def record_selection(self, photographer_username: str) -> None:
        """Bump the usage count for a photographer when the editor picks one of their photos."""
        username = (photographer_username or "").strip()
        if not username:
            return
        entry = self.stats.get(username) or PhotographerStats(username=username)
        entry.usage_count += 1
        entry.last_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.stats[username] = entry

    def score(self, photographer_username: str) -> int:
        """Return the preference score for a photographer (0 if unknown)."""
        username = (photographer_username or "").strip()
        if not username:
            return 0
        s = self.stats.get(username)
        return s.usage_count if s else 0


# ---- Recent-use history ---------------------------------------------------


@dataclass
class UsageRecord:
    asset_id: str
    last_used_at: str  # ISO
    slot_label: str = ""
    h4_text: str = ""
    article_title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class UsageHistory:
    def __init__(self, path: Path | str = DEFAULT_HISTORY_PATH):
        self.path = Path(path)
        self.records: dict[str, UsageRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for aid, v in raw.items():
            if isinstance(v, dict):
                self.records[aid] = UsageRecord(**v)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: v.to_dict() for k, v in self.records.items()}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def record_use(
        self,
        asset_id: str,
        *,
        slot_label: str = "",
        h4_text: str = "",
        article_title: str = "",
    ) -> None:
        asset_id = (asset_id or "").strip()
        if not asset_id:
            return
        self.records[asset_id] = UsageRecord(
            asset_id=asset_id,
            last_used_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            slot_label=slot_label,
            h4_text=h4_text,
            article_title=article_title,
        )

    def days_since_use(self, asset_id: str) -> float | None:
        """Return days since this asset was last used; None if never."""
        rec = self.records.get(asset_id)
        if not rec or not rec.last_used_at:
            return None
        try:
            dt = datetime.fromisoformat(rec.last_used_at)
        except Exception:
            return None
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 86400

    def is_recently_used(self, asset_id: str, *, window_days: int = RECENT_USE_WINDOW_DAYS) -> bool:
        d = self.days_since_use(asset_id)
        return d is not None and d < window_days


# ---- Scoring + ranking ----------------------------------------------------


def rank_hits(
    hits,
    *,
    preferences: "PreferencesStore | None" = None,
    history: "UsageHistory | None" = None,
    limit: int = 5,
    query_context: str = "",
):
    """Sort iStock search hits with photographer preference and recency.

    Scoring:
      + preference score (photographer usage count, max 10)
      - 30 if the asset was used within RECENT_USE_WINDOW_DAYS
      - 5 if the asset was used in the recent N months even outside the window

    The point is a *soft* nudge — we don't filter out anything, just reorder.
    """
    preferences = preferences or PreferencesStore()
    history = history or UsageHistory()

    scored = []
    for h in hits:
        score = 0
        score += min(preferences.score(getattr(h, "photographer_username", "") or ""), 10)
        score += _editorial_people_policy_score(h, query_context)
        days = history.days_since_use(getattr(h, "asset_id", "") or "")
        if days is not None:
            if days < RECENT_USE_WINDOW_DAYS:
                score -= 30  # heavy penalty: avoid short-term repeats
            elif days < RECENT_USE_WINDOW_DAYS * 2:
                score -= 5
        scored.append((score, h))
    # Stable sort: higher score first, original order preserved for ties.
    scored.sort(key=lambda kv: -kv[0])
    return [h for _, h in scored][:limit]


# 編集者ポリシー (2026-05-13 確定、再適用 2026-05-14、Policy-3 拡張 2026-05-14):
# - 人物は日本人に絞る
# - 顔はうつさない (後ろ姿 / 手元 / 足元 / シルエット を優先)
# - 抽象的な写真 / シンボル / イメージ を優先

# 明示違反 = 必ず除外 (hard_block)。alt 中に下記いずれかが含まれたら、その候補は
# 最終リストから完全に外す。日本人かどうかが alt から分からない欧米人 portrait
# でも、笑顔 / 肖像画 / カメラ目線 のキーワードがあれば確実に排除できる。
_HARD_BLOCK_TERMS = (
    # 笑顔・微笑 (face-on with positive emotion ≒ 顔出しほぼ確実)
    "笑顔", "微笑む", "微笑", "smiling", "smile",
    # "幸せな" は alt で「幸せな中年のビジネスウーマン...」のように face-on の合図に
    # なっているケースが多い (codex policy review § 4)
    "幸せな", "幸せそう",
    # 肖像 / カメラ目線
    "肖像画", "ポートレート", "portrait", "カメラ目線", "カメラを見", "looking at camera",
    "looking into the camera",
    # 明示的に欧米/白人/黒人と書かれているもの (= 日本人ではない確信)
    "白人", "欧米", "外国人", "caucasian", "western businessman", "western woman",
    "foreigner", "foreign businessman", "黒人", "アフリカ系", "african american",
    "black businessman", "black businesswoman", "black woman", "black man",
)

# 顔なし構図シグナル (積極的に boost)
_NO_FACE_INDICATORS = (
    "後ろ姿", "背中", "手元", "足元", "シルエット", "顔なし", "横顔",
    "back view", "rear view", "from behind", "hands", "feet", "silhouette", "no face",
    "headless", "faceless",
)

# 人物指標 (人物が写ってる可能性が高い)
_PEOPLE_INDICATORS = (
    "人", "人物", "男", "女", "ポートレート", "顔",
    "people", "person", "man", "woman", "men", "women",
    "businessman", "businesswoman", "businesspeople",
    "boy", "girl", "child", "kid",
    "family", "team", "group", "couple", "colleague",
)

# 日本人指標
_JAPANESE_INDICATORS = (
    "日本人", "日本の", "日本のビジネス", "japanese", "asian",
)

# 抽象 / シンボル / グラフ (積極的に boost)
_ABSTRACT_INDICATORS = (
    "グラフ", "矢印", "チャート", "シンボル", "ランドマーク", "街並み", "都市",
    "建物", "ビル", "風景", "イメージ", "概念", "図形", "アイコン",
    "graph", "chart", "arrow", "icon", "skyline", "landmark", "cityscape",
    "concept", "abstract", "infographic", "diagram",
)


@dataclass(frozen=True)
class PolicyEval:
    """編集部写真ポリシーに対する 1 候補の評価結果。"""

    score: int  # -100..+20 程度の生スコア。
    hard_block: bool  # True なら最終リストから除外。
    ambiguous_person: bool  # 人物だが日本人/顔なし指標が無く要警戒。
    reasons: list[str]  # デバッグ用 (どの語が当たったか)


def evaluate_editorial_people_policy(hit, query_context: str = "") -> PolicyEval:
    """編集部ポリシーで候補を評価して PolicyEval を返す。

    hard_block: True なら呼び出し側は表示前に除外する責任を持つ。
    score: 通常スコア (rank_hits 等のソートに使える)。
    ambiguous_person: 人物指標はあるが日本人 / 顔なし指標が無い候補。reranker で
                     soft-demote の対象。
    """
    alt = (getattr(hit, "alt", "") or "").lower()
    detail = (getattr(hit, "detail_url", "") or "").lower()
    haystack = f"{alt} {detail}"

    # 1) hard-block 判定 (明示違反)
    hits_blocked: list[str] = []
    for term in _HARD_BLOCK_TERMS:
        if term in haystack:
            hits_blocked.append(term)
    if hits_blocked:
        return PolicyEval(
            score=-100,
            hard_block=True,
            ambiguous_person=False,
            reasons=[f"hard_block:{t}" for t in hits_blocked],
        )

    reasons: list[str] = []
    is_no_face = any(t in haystack for t in _NO_FACE_INDICATORS)
    is_people = any(t in haystack for t in _PEOPLE_INDICATORS)
    is_japanese = any(t in haystack for t in _JAPANESE_INDICATORS)
    is_abstract = any(t in haystack for t in _ABSTRACT_INDICATORS)

    score = 0

    # 抽象 / シンボル / グラフ — 編集部ポリシー 3 で積極的に boost
    if is_abstract:
        score += 10
        reasons.append("abstract")

    # 日本人と分かる → +8
    if is_japanese:
        score += 8
        reasons.append("japanese")

    # 顔なし構図 → +8
    if is_no_face:
        score += 8
        reasons.append("no_face_composition")

    # ambiguous: 人物が写ってるが日本人 / 顔なし のシグナルが無い → soft demote
    ambiguous = False
    if is_people and not is_japanese and not is_no_face and not is_abstract:
        score -= 6
        reasons.append("ambiguous_person")
        ambiguous = True

    # 「顔」が明示されている (顔のクローズアップ等) → -8
    # 「顔なし」は _NO_FACE_INDICATORS で boost 済なので、その判定後にしか減点しない
    if not is_no_face and any(t in haystack for t in ("顔のアップ", "顔のクローズ", "顔出し", "face close")):
        score -= 8
        reasons.append("face_closeup")

    return PolicyEval(
        score=score,
        hard_block=False,
        ambiguous_person=ambiguous,
        reasons=reasons,
    )


def _editorial_people_policy_score(hit, query_context: str) -> int:
    """Backwards-compatible scalar API used by rank_hits.

    hard_block 判定は呼び出し側に伝えられないので、強い負スコア (-100) を返して
    rank_hits の安全弁にする。最終的な hard_block は candidate_reranker 側で
    evaluate_editorial_people_policy() を直接呼んで除外する。
    """
    evaluation = evaluate_editorial_people_policy(hit, query_context)
    return evaluation.score
