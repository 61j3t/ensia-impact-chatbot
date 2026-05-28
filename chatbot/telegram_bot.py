"""Telegram frontend for the ENSIA Impact chatbot.

Reads TELEGRAM_BOT_TOKEN from .env and long-polls Telegram for messages.
Each user message is forwarded to chatbot.answer.answer() and the result
is replied with citations.

Behavior:
  • Private chat:   responds to every text message.
  • Group chat:     responds only when the bot is @mentioned, when the
                    user replies to one of the bot's messages, or when
                    the message starts with /ask.
  • Commands:       /start, /help, /ask <question>

Models are loaded ONCE at startup (BGE-M3 + reranker = ~3 GB RAM, ~10 s
to initialise). All subsequent requests reuse the same Retriever, so the
typical reply latency is dominated by the LLM call (1–3 s on Groq).

Usage:
  PYTHONPATH=. .venv/bin/python -m chatbot.telegram_bot
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from chatbot.answer import answer
from chatbot.memory import ConversationMemory
from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ENSIA Impact supergroup. Used to build deep links like
#   https://t.me/c/<CHAT_ID>/<topic_id>/<message_id>
ENSIA_CHAT_ID = 2482670091
MESSAGES_JSON = ROOT / "data/messages_enriched.json"
PDF_SOURCE_DIR = ROOT / "data/chats/chat_1/files"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Quiet down httpx — it logs every Telegram poll.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ensia.bot")

# Loaded once at startup, shared across all requests.
_retriever: Retriever | None = None
_memory: ConversationMemory | None = None
_bot_username: str | None = None
_topic_id_by_msg: dict[int, int] = {}  # message_id → topic_created msg id
_pdf_path_by_metakey: dict[str, Path] = {}  # metadata pdf_file (.txt) → original .pdf path
_msg_id_by_pdf_name: dict[str, int] = {}    # original PDF filename → Telegram msg id that shared it

# How many recent exchanges to keep / replay per conversation.
HISTORY_TURNS = 5
HISTORY_TTL_HOURS = 24.0

# Per-user rate limit. A user must wait this many seconds between queries,
# measured from when they sent the last one. Cheap anti-spam — the dict
# grows with the user count but the entries are tiny.
USER_COOLDOWN_S = 5.0
_last_query_time: dict[int, float] = {}


def _build_topic_id_map() -> dict[int, int]:
    """Walk reply chains to map every content msg id → its topic-thread id.

    The topic id we want is the message id of the corresponding
    `topic_created` service message, because Telegram's deep link uses
    that as the thread anchor: t.me/c/<chat>/<topic_id>/<msg_id>.
    """
    with open(MESSAGES_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    messages = raw["chats"]["list"][0]["messages"]

    topic_ids = {m["id"] for m in messages if m.get("action") == "topic_created"}
    by_id = {m["id"]: m for m in messages}

    result: dict[int, int] = {}
    for m in messages:
        if m.get("type") != "message":
            continue
        cur = m
        visited: set[int] = set()
        while cur:
            rid = cur.get("reply_to_message_id")
            if rid is None:
                break
            if rid in topic_ids:
                result[m["id"]] = rid
                break
            if rid in visited:
                break
            visited.add(rid)
            cur = by_id.get(rid)
    return result


def _telegram_link(message_id: int) -> str:
    """Construct a deep link to a specific message. Falls back to a
    chat-level link when the message has no resolvable topic thread."""
    topic_id = _topic_id_by_msg.get(message_id)
    if topic_id is None:
        return f"https://t.me/c/{ENSIA_CHAT_ID}/{message_id}"
    return f"https://t.me/c/{ENSIA_CHAT_ID}/{topic_id}/{message_id}"


def _build_pdf_path_map() -> dict[str, Path]:
    """Map a chunk's `pdf_file` (e.g. 'Arreté_008.txt') back to its
    original PDF on disk. The extract/OCR scripts produce .txt names by
    replacing non-word/non-dash chars with underscores in the stem; we
    replay that here.

    Critical: macOS APFS hands us NFD-decomposed filenames (e.g. é = e +
    U+0301) and Python's \\w does NOT match U+0301, so we must normalize
    to NFC *before* the regex or diacritics get stripped.
    """
    out: dict[str, Path] = {}
    if not PDF_SOURCE_DIR.exists():
        return out
    for pdf in PDF_SOURCE_DIR.glob("*.pdf"):
        stem = unicodedata.normalize("NFC", pdf.stem)
        stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
        out[stem + ".txt"] = pdf
    return out


def _resolve_pdf_path(pdf_file_meta: str | None) -> Path | None:
    if not pdf_file_meta:
        return None
    return _pdf_path_by_metakey.get(unicodedata.normalize("NFC", pdf_file_meta))


def _build_pdf_msg_map() -> dict[str, int]:
    """For each PDF that was shared in the chat, map its original filename
    to the Telegram message id that posted it. Used to deep-link source
    rows for PDFs back to the actual chat message that contains the file."""
    out: dict[str, int] = {}
    if not MESSAGES_JSON.exists():
        return out
    with open(MESSAGES_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    for m in raw["chats"]["list"][0]["messages"]:
        if m.get("type") != "message":
            continue
        file_field = m.get("file")
        if not file_field or not str(file_field).lower().endswith(".pdf"):
            continue
        name = Path(file_field).name
        out[unicodedata.normalize("NFC", name)] = m["id"]
    return out


def _msg_id_for_pdf(original_pdf_name: str | None) -> int | None:
    if not original_pdf_name:
        return None
    return _msg_id_by_pdf_name.get(unicodedata.normalize("NFC", original_pdf_name))


def _pretty_pdf_name(pdf_file_meta: str | None) -> str:
    """Return the original PDF filename (with spaces, accents) when we
    can resolve it, else fall back to the safe-name in metadata."""
    if not pdf_file_meta:
        return "?"
    p = _resolve_pdf_path(pdf_file_meta)
    return p.name if p else pdf_file_meta


WELCOME = (
    "Salam 👋  I'm the *ENSIA Impact* assistant.\n\n"
    "I answer questions grounded in everything shared on the ENSIA Impact "
    "Telegram server — chat messages, PDFs, OCR'd images, plus the "
    "official ensia.edu.dz and v2v.ensia.edu.dz pages and every link "
    "students have shared.\n\n"
    "*Things you can ask:*\n"
    "• Which companies has ENSIA partnered with?\n"
    "• What projects are currently incubated at the CDE?\n"
    "• Why join the incubator — what does it offer?\n"
    "• What types of final-year projects exist and how do I pick one?\n"
    "• How do I register a startup under décret 1275?\n"
    "• Quels événements arrivent ce mois-ci?\n"
    "• ما هي شروط الانضمام إلى الحاضنة؟\n\n"
    "I reply in English, French, or Arabic; I cite my sources; and I'll "
    "say so when I don't know. Hit /help for usage tips."
)

HELP = (
    "*How to use this bot*\n\n"
    "• In a *direct message*: just type your question.\n"
    "• In a *group*: mention me (@ensia_impact_group_bot), reply to one "
    "of my messages, or use /ask `<your question>`.\n"
    "• I remember the last few exchanges so follow-ups like \"and how do "
    "I apply?\" work. Use /reset to clear memory.\n\n"
    "Examples:\n"
    "• What is the CDE at ENSIA?\n"
    "• How do I register a startup under decree 1275?\n"
    "• Comment soumettre un PFE comme projet startup?\n"
    "• هل تنصحني بالدراسة في الخارج؟"
)


# ─── handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user or _memory is None:
        return
    deleted = _memory.reset(chat_id, user.id)
    if deleted > 0:
        await update.effective_message.reply_text(
            f"✅ Conversation memory cleared ({deleted // 2} exchanges)."
        )
    else:
        await update.effective_message.reply_text(
            "Nothing to clear — no previous conversation found."
        )


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "Usage: /ask <your question>\n\nExample: /ask What is the CDE at ENSIA?"
        )
        return
    await _handle_query(update, context, query)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route plain text messages, applying group-chat scoping rules."""
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_type = update.effective_chat.type
    text = msg.text.strip()
    bot_username = _bot_username or ""

    if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
        # Only respond when explicitly addressed.
        addressed = False
        # @mention
        if f"@{bot_username}" in text:
            addressed = True
            text = text.replace(f"@{bot_username}", "").strip()
        # Reply to one of the bot's previous messages
        if (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.username == bot_username
        ):
            addressed = True
        if not addressed:
            return

    if not text:
        return

    # If this user just tapped 🚩 Report and hasn't given a reason yet,
    # treat the next text message as the reason instead of a query.
    if _memory is not None and update.effective_user is not None:
        try:
            pending = await asyncio.to_thread(
                _memory.pending_report,
                update.effective_chat.id,
                update.effective_user.id,
            )
        except Exception:
            logger.exception("pending_report check failed")
            pending = None
        if pending is not None:
            feedback_id, _reported_msg = pending
            try:
                await asyncio.to_thread(
                    _memory.add_report_reason, feedback_id, text[:500],
                )
                await msg.reply_text(
                    "🙏 Thanks — your report has been saved."
                )
                logger.info(
                    "report reason saved for feedback id %d by user %d",
                    feedback_id, update.effective_user.id,
                )
                return
            except Exception:
                logger.exception("add_report_reason failed")
                # Fall through and treat it as a normal query.

    await _handle_query(update, context, text)


async def _handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None

    # Per-user cooldown. Reject quickly so we don't queue up retrieval work
    # for spammers. Counts only "real" queries — failed cooldown checks
    # don't update the timestamp, so users can't lock themselves out by
    # spamming faster than the cooldown.
    now = time.monotonic()
    last = _last_query_time.get(user_id, 0.0)
    if now - last < USER_COOLDOWN_S:
        wait_s = USER_COOLDOWN_S - (now - last)
        logger.info(
            "rate-limited @%s in chat %s (%.1fs left)",
            username, chat_id, wait_s,
        )
        await update.effective_message.reply_text(
            f"⏳ Slow down — try again in {wait_s:.0f}s."
        )
        return
    _last_query_time[user_id] = now

    logger.info("query from @%s in chat %s: %r", username, chat_id, query[:80])

    # React with 🤔 on the user's message so they get instant acknowledgement
    # that we picked it up — useful when the LLM takes a few seconds. The
    # reaction is cleared right before we send the reply (see below). Best-
    # effort: a missing/unsupported chat shouldn't block the answer flow.
    incoming_msg = update.effective_message
    reaction_set = False
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=incoming_msg.message_id,
            reaction=[ReactionTypeEmoji(emoji="🤔")],
        )
        reaction_set = True
    except Exception:
        logger.debug("set_message_reaction failed", exc_info=True)

    # Record / refresh the user's metadata + bump their query counter.
    # Best-effort: a Postgres outage shouldn't prevent answering.
    if _memory is not None and user is not None:
        try:
            _memory.upsert_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code,
                is_bot=bool(user.is_bot),
            )
            _memory.increment_query_count(user.id)
        except Exception:
            logger.exception("memory.upsert_user / increment_query_count failed")

    # Pull recent conversation history for this (chat, user). Memory is
    # per-user even in groups so different students don't share context.
    history: list[dict] = []
    if _memory is not None:
        try:
            history = _memory.recent_turns(
                chat_id, user_id,
                n=HISTORY_TURNS, max_age_hours=HISTORY_TTL_HOURS,
            )
        except Exception:
            logger.exception("memory.recent_turns failed; proceeding without history")

    # Show "typing…" while we work.
    typing_task = asyncio.create_task(_keep_typing(context, chat_id))
    t_start = time.monotonic()
    try:
        result = await asyncio.to_thread(
            answer, query, retriever=_retriever, history=history,
        )
    except Exception:
        logger.exception("answer() failed")
        typing_task.cancel()
        await _clear_reaction(context, chat_id, incoming_msg.message_id, reaction_set)
        await update.effective_message.reply_text(
            "Something went wrong on my side. Try again in a moment."
        )
        return
    finally:
        typing_task.cancel()
    elapsed_s = time.monotonic() - t_start
    timings = result.get("timings") or {}

    logger.info(
        "answered total=%.1fs · rewrite=%.1fs · retrieval=%.1fs · answer=%.1fs · "
        "tier=%s · score=%.3f%s",
        elapsed_s,
        timings.get("rewrite", 0.0),
        timings.get("retrieval", 0.0),
        timings.get("answer", 0.0),
        result["tier"],
        result["top_score"],
        f" · rewritten=({result['retrieval_query']!r})"
        if result.get("retrieval_query") else "",
    )

    reply = _format_reply(result, elapsed_s)
    # Telegram caps a single message at 4096 chars; trim defensively.
    if len(reply) > 4000:
        reply = reply[:4000].rstrip() + "…"

    # Attach the feedback keyboard to every real answer. Refusals get a
    # 👎 / 🚩 keyboard so users can still flag a bad refusal (no 👍 — no
    # answer to be useful), but small talk and rate-limit replies don't
    # get a keyboard at all (those go through reply_text directly).
    keyboard = _feedback_keyboard(refusal=bool(result.get("refused")))

    sent_msg = None
    try:
        sent_msg = await update.effective_message.reply_text(
            reply,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("HTML send failed; retrying as plain text")
        plain = f"{result['answer']}\n\nSources: " + ", ".join(
            s["id"] for s in result.get("sources") or []
        )
        sent_msg = await update.effective_message.reply_text(
            plain[:4000], reply_markup=keyboard
        )

    # Reply has landed — clear the 🤔 thinking reaction.
    await _clear_reaction(context, chat_id, incoming_msg.message_id, reaction_set)

    # Persist the exchange to memory only when the bot actually answered.
    # We skip refusals (hard refuse, LLM timeout, "can't find") so they
    # don't pollute future context.
    if _memory is not None and not result.get("refused"):
        try:
            _memory.add_turns(chat_id, user_id, [
                {"role": "user", "content": query},
                {
                    "role": "assistant",
                    "content": result["answer"],
                    "sources": result.get("sources") or None,
                    "tg_message_id": sent_msg.message_id if sent_msg else None,
                },
            ])
        except Exception:
            logger.exception("memory.add_turns failed")


# ─── inline feedback keyboard ──────────────────────────────────────────────

# callback_data is capped at 64 bytes by Telegram; "fb:useful" etc. fits
# easily. We don't need to encode chat/message ids — those come from the
# CallbackQuery itself.
_CB_USEFUL = "fb:useful"
_CB_NOT_USEFUL = "fb:not_useful"
_CB_REPORT = "fb:report"


def _feedback_keyboard(refusal: bool = False) -> InlineKeyboardMarkup:
    """Three-button row attached under every assistant reply. On refusal
    we hide 👍 (nothing to be useful about) but keep 👎 / 🚩."""
    row = []
    if not refusal:
        row.append(InlineKeyboardButton("👍 Useful", callback_data=_CB_USEFUL))
    row.append(InlineKeyboardButton("👎 Not useful", callback_data=_CB_NOT_USEFUL))
    row.append(InlineKeyboardButton("🚩 Report", callback_data=_CB_REPORT))
    return InlineKeyboardMarkup([row])


_RATING_LABEL = {
    "useful": "👍 Marked useful",
    "not_useful": "👎 Marked not useful",
    "report": "🚩 Reported — reply with a short reason (or ignore to skip)",
    "updated": "✓ Updated",
    "removed": "↩ Cleared",
}


async def on_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle taps on the 👍 / 👎 / 🚩 buttons under bot replies."""
    cq = update.callback_query
    if cq is None or not cq.data or not cq.data.startswith("fb:"):
        return
    rating = cq.data.split(":", 1)[1]
    if rating not in ("useful", "not_useful", "report"):
        await cq.answer("Unknown feedback type")
        return

    user_id = cq.from_user.id
    chat_id = cq.message.chat.id
    message_id = cq.message.message_id

    if _memory is None:
        await cq.answer("Feedback storage unavailable", show_alert=False)
        return

    try:
        outcome = await asyncio.to_thread(
            _memory.set_feedback, chat_id, message_id, user_id, rating,
        )
    except Exception:
        logger.exception("set_feedback failed")
        await cq.answer("Couldn't record that — try again", show_alert=False)
        return

    label = _RATING_LABEL.get(outcome) or _RATING_LABEL.get(rating, "✓")
    # Toast popup on the user's client.
    await cq.answer(label)

    # Replace the keyboard with the current state: pressed button gets a
    # leading "✓". Toggling off restores the full unchecked keyboard.
    if outcome == "removed":
        await cq.edit_message_reply_markup(
            reply_markup=_feedback_keyboard(refusal=False)
        )
    else:
        await cq.edit_message_reply_markup(
            reply_markup=_keyboard_with_active(rating)
        )

    logger.info(
        "feedback %s by user %d on (chat=%d msg=%d): outcome=%s",
        rating, user_id, chat_id, message_id, outcome,
    )


def _keyboard_with_active(active: str) -> InlineKeyboardMarkup:
    """Show which rating the user gave — leading ✓ on the chosen button."""
    def lbl(text: str, key: str) -> str:
        return f"✓ {text}" if key == active else text
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            lbl("👍 Useful", "useful"), callback_data=_CB_USEFUL,
        ),
        InlineKeyboardButton(
            lbl("👎 Not useful", "not_useful"), callback_data=_CB_NOT_USEFUL,
        ),
        InlineKeyboardButton(
            lbl("🚩 Report", "report"), callback_data=_CB_REPORT,
        ),
    ]])


async def _clear_reaction(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    was_set: bool,
) -> None:
    """Remove the 🤔 reaction the bot left on the user's message at the
    start of a turn. No-op when the reaction couldn't be set in the first
    place (e.g. an older chat that doesn't allow bot reactions)."""
    if not was_set:
        return
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=[]
        )
    except Exception:
        logger.debug("clear_message_reaction failed", exc_info=True)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Send TYPING action every ~4 s until cancelled (Telegram action lasts 5 s)."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return


def _fmt_tokens(n: int) -> str:
    """Compact token counts for the footer: 12345 -> '12k', 980 -> '980'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _format_reply(result: dict, elapsed_s: float) -> str:
    """Render the answer + sources block as HTML for Telegram."""
    answer_text = html.escape(result["answer"])
    parts = [answer_text]

    # Footer: wall-clock + context-window fill, like Claude's "X% of
    # context used" indicator. tokens / max_input_tokens for the model.
    used = result.get("context_tokens") or 0
    cap = result.get("context_max")
    ctx_bit = ""
    if used > 0 and cap:
        pct = round(used / cap * 100)
        ctx_bit = f" · 🧠 {pct}% ({_fmt_tokens(used)}/{_fmt_tokens(cap)})"
    elif used > 0:
        ctx_bit = f" · 🧠 {_fmt_tokens(used)} tokens"
    timing_line = f"<i>⏱ {elapsed_s:.1f}s{ctx_bit}</i>"

    if result["refused"]:
        parts.append("\n" + timing_line)
        return "\n".join(parts)

    sources = result.get("sources") or []
    if sources:
        parts.append("\n<b>📚 Sources</b>")
        for s in sources:
            md = s["metadata"]
            num = s.get("number")
            tag = f"[{num}]" if num is not None else "•"
            kind = md.get("source_type")
            if kind == "chat":
                topic = html.escape(md.get("topic") or "—")
                date = (md.get("date") or "")[:10]
                msg_id = md.get("message_id")
                link = _telegram_link(msg_id) if isinstance(msg_id, int) else None
                label = f"{topic}" + (f" · {date}" if date else "")
                if link:
                    label = f'<a href="{html.escape(link)}">{label}</a>'
                parts.append(f"{tag} {label}")
            elif kind == "external":
                url = md.get("url") or ""
                title = html.escape(md.get("title") or url)
                label = f"🌐 {title}"
                if url:
                    label = f'<a href="{html.escape(url)}">{label}</a>'
                parts.append(f"{tag} {label}")
            else:  # pdf
                pdf_name = _pretty_pdf_name(md.get("pdf_file"))
                pdf_msg_id = _msg_id_for_pdf(pdf_name)
                label = f"📄 {html.escape(pdf_name)}"
                if pdf_msg_id is not None:
                    link = _telegram_link(pdf_msg_id)
                    label = f'<a href="{html.escape(link)}">{label}</a>'
                parts.append(f"{tag} {label}")

    parts.append("\n" + timing_line)
    return "\n".join(parts)


# ─── error handler ──────────────────────────────────────────────────────────

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("update %s caused exception", update, exc_info=context.error)


# ─── entry point ────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Cache the bot's username once after the bot is initialized.

    Failures here are non-fatal — the bot can still serve direct messages
    without knowing its own username; it just won't auto-detect mentions
    in group chats until a successful retry.
    """
    global _bot_username
    try:
        me = await app.bot.get_me()
        _bot_username = me.username
        logger.info("Bot identity: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        logger.warning("get_me() failed at startup (%s) — group mentions disabled until next call.", e)


def main() -> None:
    global _retriever, _memory, _topic_id_by_msg, _pdf_path_by_metakey, _msg_id_by_pdf_name
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")

    logger.info("Building topic-link map…")
    _topic_id_by_msg = _build_topic_id_map()
    logger.info("Topic map: %d messages have a resolvable topic thread", len(_topic_id_by_msg))

    _pdf_path_by_metakey = _build_pdf_path_map()
    logger.info("PDF map: %d PDFs available", len(_pdf_path_by_metakey))

    _msg_id_by_pdf_name = _build_pdf_msg_map()
    logger.info("PDF→msg map: %d PDFs are linkable to their share message", len(_msg_id_by_pdf_name))

    _memory = ConversationMemory()
    # Hide the password from logs in case anyone runs the bot interactively.
    safe_dsn = re.sub(r":[^:@/]+@", ":***@", _memory.dsn)
    logger.info("Conversation memory ready (postgres %s)", safe_dsn)

    logger.info("Loading retriever (BGE-M3 + reranker)… this takes ~10 s")
    _retriever = Retriever()
    # Warm up the lazy-loaded models so first user request isn't a cold start.
    _retriever.search("hello", k=1, rerank=True)
    logger.info("Retriever ready")

    app = (
        Application.builder()
        .token(token)
        # Bigger timeouts — defaults (5 s) sometimes flake from this network.
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^fb:"))
    app.add_error_handler(on_error)

    logger.info("Bot starting (long-polling). Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
