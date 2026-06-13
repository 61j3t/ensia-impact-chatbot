"""Badges + stats for the /me command.

A badge is just a (key, name, description, emoji, predicate) tuple. The
predicate is a pure function over the stats dict returned by
`ConversationMemory.get_user_stats()` — so adding new badges later is
just appending an entry here, no schema changes.

We don't expose hidden milestones the user has yet to earn — `/me`
shows earned badges + a brief "next up" hint to give a sense of
progression without becoming a guide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Badge:
    key: str
    name: str
    description: str
    emoji: str
    earned: Callable[[dict], bool]


# Cheap language detection: just check if any chars belong to the
# script. Good enough for "did this user ever ask in Arabic / French?".
_AR_RE = re.compile(r"[؀-ۿݐ-ݿ]")
_FR_RE = re.compile(r"[àâçéèêëîïôûùüÿœæÀÂÇÉÈÊËÎÏÔÛÙÜŸŒÆ]")


def _languages(queries: list[str]) -> set[str]:
    seen: set[str] = set()
    for q in queries:
        if _AR_RE.search(q):
            seen.add("ar")
        if _FR_RE.search(q):
            seen.add("fr")
        # Heuristic: if the query has more ASCII letters than not and
        # neither French/Arabic, count as English.
        if (not _AR_RE.search(q) and not _FR_RE.search(q)
                and any(c.isalpha() and c.isascii() for c in q)):
            seen.add("en")
    return seen


BADGES: list[Badge] = [
    Badge(
        "curious",
        "Curious Mind",
        "Asked your first question",
        "🎯",
        lambda s: s.get("total_q", 0) >= 1,
    ),
    Badge(
        "scholar_25",
        "Scholar",
        "Asked 25+ questions",
        "🎓",
        lambda s: s.get("total_q", 0) >= 25,
    ),
    Badge(
        "scholar_100",
        "Veteran Scholar",
        "Asked 100+ questions",
        "📚",
        lambda s: s.get("total_q", 0) >= 100,
    ),
    Badge(
        "scholar_500",
        "Knowledge Seeker",
        "Asked 500+ questions",
        "🧠",
        lambda s: s.get("total_q", 0) >= 500,
    ),
    Badge(
        "polyglot",
        "Polyglot",
        "Asked in English, French, and Arabic",
        "🌍",
        lambda s: {"en", "ar", "fr"}.issubset(_languages(s.get("queries") or [])),
    ),
    Badge(
        "deep_diver",
        "Deep Diver",
        "Used /deep 5 times for careful answers",
        "🤿",
        lambda s: s.get("deep_q", 0) >= 5,
    ),
    Badge(
        "night_owl",
        "Night Owl",
        "Asked 10 questions between midnight and 5 AM",
        "🦉",
        lambda s: s.get("night_q", 0) >= 10,
    ),
    Badge(
        "early_bird",
        "Early Bird",
        "Asked 10 questions before 8 AM",
        "🌅",
        lambda s: s.get("morn_q", 0) >= 10,
    ),
    Badge(
        "researcher",
        "Researcher",
        "Got 10 answers backed by PDF sources",
        "🔬",
        lambda s: s.get("pdf_cited", 0) >= 10,
    ),
    Badge(
        "helper",
        "Helper",
        "Gave 10 helpful 👍 reactions to bot answers",
        "🗣️",
        lambda s: s.get("helpful_votes", 0) >= 10,
    ),
    Badge(
        "on_fire",
        "On Fire",
        "Maintained a 7-day question streak",
        "🔥",
        lambda s: s.get("best_streak", 0) >= 7,
    ),
    Badge(
        "topic_explorer",
        "Topic Explorer",
        "Got answers from 5+ different ENSIA channels",
        "🗺️",
        lambda s: len(set(s.get("top_topics") or [])) >= 5,
    ),
]
BADGES_BY_KEY = {b.key: b for b in BADGES}


def evaluate_badges(stats: dict) -> list[Badge]:
    """Return the badges the user has currently earned, in BADGES order."""
    return [b for b in BADGES if b.earned(stats)]


def newly_earned(stats: dict, already_earned: list[str]) -> list[Badge]:
    """Subset of evaluate_badges that wasn't in `already_earned` before."""
    keys = set(already_earned or [])
    return [b for b in evaluate_badges(stats) if b.key not in keys]


def format_me_card(stats: dict, earned: list[Badge]) -> str:
    """Render the /me reply text in Telethon-flavored markdown."""
    from datetime import datetime, timezone

    name = (stats.get("first_name") or "").strip() or stats.get("username") or "there"
    total = stats.get("total_q", 0)
    week = stats.get("week_q", 0)
    streak = stats.get("current_streak", 0)
    best = stats.get("best_streak", 0)
    joined = stats.get("joined_at")
    member_for = ""
    if joined:
        if isinstance(joined, str):
            joined_dt = datetime.fromisoformat(joined.replace("Z", "+00:00"))
        else:
            joined_dt = joined
        days = (datetime.now(timezone.utc) - joined_dt).days
        member_for = f"{days} day{'s' if days != 1 else ''}" if days < 60 else f"{days // 30} month{'s' if days // 30 != 1 else ''}"

    lines = [f"Hello, **{name}**! 👋"]
    lines.append("")
    lines.append("**📊 Your stats**")
    lines.append(f"  · {total} question{'s' if total != 1 else ''} asked")
    if week:
        lines.append(f"  · {week} this week")
    if member_for:
        lines.append(f"  · member for {member_for}")
    if streak:
        lines.append(
            f"  · 🔥 {streak}-day streak"
            + (f" (best: {best})" if best > streak else "")
        )
    lines.append("")

    lines.append(f"**🏆 Badges ({len(earned)} / {len(BADGES)})**")
    if earned:
        for b in earned:
            lines.append(f"  {b.emoji} {b.name} — _{b.description}_")
    else:
        lines.append("  _no badges yet — ask a question to start!_")
    lines.append("")

    topics = stats.get("top_topics") or []
    if topics:
        lines.append("**📚 Top topics you've explored**")
        lines.append("  · " + ", ".join(topics))
        lines.append("")

    # Subtle nudge — what's the NEXT unearned badge that's closest?
    unearned = [b for b in BADGES if not b.earned(stats)]
    if unearned:
        lines.append(f"_Next up:_ {unearned[0].emoji} **{unearned[0].name}** — {unearned[0].description.lower()}")

    return "\n".join(lines)
