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


def _editorial_people_policy_score(hit, query_context: str) -> int:
    context = (query_context or "").lower()
    if not any(
        token in context
        for token in (
            "日本人",
            "顔なし",
            "後ろ姿",
            "手元",
            "japanese",
            "no face",
            "back view",
            "hands",
        )
    ):
        return 0

    alt = (getattr(hit, "alt", "") or "").lower()
    detail = (getattr(hit, "detail_url", "") or "").lower()
    haystack = f"{alt} {detail}"
    score = 0
    if any(token in haystack for token in ("日本人", "日本の", "japanese", "asian")):
        score += 8
    if any(token in haystack for token in ("後ろ姿", "背中", "手元", "足元", "シルエット", "顔なし", "back view", "hands", "feet", "silhouette", "no face")):
        score += 8
    if any(token in haystack for token in ("顔", "笑顔", "ポートレート", "カメラ目線", "face", "smiling", "portrait", "looking at camera")):
        score -= 8
    if any(token in haystack for token in ("白人", "欧米", "外国人", "caucasian", "western")):
        score -= 10
    return score
