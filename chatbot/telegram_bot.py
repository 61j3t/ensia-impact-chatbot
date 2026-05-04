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
from telegram import Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (
    Application,
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
    "Salam 👋  I'm the ENSIA Impact assistant.\n\n"
    "Ask me anything about content shared in the ENSIA Impact Telegram "
    "server — startups, internships, decree 1275, the incubator/CDE, "
    "events, and more.\n\n"
    "I answer in English, French, or Arabic. I'll cite my sources, and "
    "I'll tell you when I don't know."
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
    await update.effective_message.reply_text(WELCOME)


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

    await _handle_query(update, context, text)


async def _handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    logger.info("query from @%s in chat %s: %r", username, chat_id, query[:80])

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
    try:
        await update.effective_message.reply_text(
            reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception:
        logger.exception("HTML send failed; retrying as plain text")
        plain = f"{result['answer']}\n\nSources: " + ", ".join(s["id"] for s in result["sources"])
        await update.effective_message.reply_text(plain[:4000])

    # Persist the exchange to memory only when the bot actually answered.
    # We skip refusals (hard refuse, LLM timeout, "can't find") so they
    # don't pollute future context.
    if _memory is not None and not result.get("refused"):
        try:
            _memory.add_turns(chat_id, user_id, [
                {"role": "user", "content": query},
                {"role": "assistant", "content": result["answer"]},
            ])
        except Exception:
            logger.exception("memory.add_turns failed")


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Send TYPING action every ~4 s until cancelled (Telegram action lasts 5 s)."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return


def _format_reply(result: dict, elapsed_s: float) -> str:
    """Render the answer + sources block as HTML for Telegram."""
    answer_text = html.escape(result["answer"])
    parts = [answer_text]
    timing_line = f"<i>⏱ {elapsed_s:.1f}s</i>"

    if result["refused"]:
        parts.append("\n" + timing_line)
        return "\n".join(parts)

    sources = result.get("sources") or []
    if sources:
        parts.append("\n<b>📚 Sources</b>")
        for s in sources:
            md = s["metadata"]
            sid = html.escape(s["id"])
            if md.get("source_type") == "chat":
                topic = html.escape(md.get("topic") or "—")
                date = (md.get("date") or "")[:10]
                msg_id = md.get("message_id")
                link = _telegram_link(msg_id) if isinstance(msg_id, int) else None
                label_left = (
                    f'<a href="{html.escape(link)}">{sid}</a>'
                    if link else f"<code>{sid}</code>"
                )
                where = f"{topic}" + (f" · {date}" if date else "")
                parts.append(f"• {label_left} — {where}")
            else:
                pdf_name = _pretty_pdf_name(md.get("pdf_file"))
                pdf_msg_id = _msg_id_for_pdf(pdf_name)
                if pdf_msg_id is not None:
                    link = _telegram_link(pdf_msg_id)
                    label_left = f'<a href="{html.escape(link)}">{sid}</a>'
                else:
                    label_left = f"<code>{sid}</code>"
                parts.append(f"• {label_left} — 📄 {html.escape(pdf_name)}")

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
    logger.info("Conversation memory ready (sqlite at %s)", _memory.db_path.relative_to(ROOT))

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
    app.add_error_handler(on_error)

    logger.info("Bot starting (long-polling). Ctrl-C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
