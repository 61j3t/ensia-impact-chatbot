"""Telethon (MTProto) port of the bot — experimental.

The python-telegram-bot reference implementation in
`chatbot.telegram_bot` talks to api.telegram.org (HTTPS Bot API). On
free Hugging Face Spaces that hostname is blocked, so this module
re-implements the same surface area on top of Telethon, which speaks
MTProto directly to Telegram's data-center IPs (149.154.x.x). Those IPs
are NOT blocked by the same egress policy — at least, that's the
hypothesis this branch is testing.

Run it instead of the legacy bot with the same env:
    PYTHONPATH=. .venv/bin/python -m chatbot.telegram_bot_telethon

Differences vs. the legacy bot:
  • Auth: Telethon needs api_id + api_hash + bot_token (we already
    have all three for the user-account sync; bot_token is reused).
  • Reactions: Telethon's `SendReactionRequest` lets us send 🤔 with
    no allowlist surprises (legacy bot's reaction API rejected 💭).
  • Inline keyboards: `Button.inline(text, data)` and `events.CallbackQuery`.
  • The answer pipeline, memory layer, sources rendering, feedback
    storage and the "context-fill %" footer are all reused unchanged
    from the legacy module.

Same secrets as the legacy bot:
    TELEGRAM_BOT_TOKEN   bot account token from @BotFather
    TELEGRAM_API_ID      api credentials (https://my.telegram.org/apps)
    TELEGRAM_API_HASH
    GROQ_API_KEY         passed through to litellm
    DATABASE_URL         Neon Postgres
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
from telethon import Button, TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    ReactionEmoji,
)

from chatbot.answer import answer
from chatbot.memory import ConversationMemory
from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ENSIA_CHAT_ID = 2482670091
MESSAGES_JSON = ROOT / "data/messages_enriched.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("ensia.bot.telethon")

# Singletons loaded once at startup.
_retriever: Retriever | None = None
_memory: ConversationMemory | None = None
_bot_username: str | None = None
_topic_id_by_msg: dict[int, int] = {}
_msg_id_by_pdf_name: dict[str, int] = {}
# Map a chunk's `pdf_file` metadata (the .txt name produced by
# 01_extract_pdfs.py, e.g. "Arrêté_1275.txt") → the original PDF name +
# the Telegram message id that shared it. Built at startup from
# messages_enriched.json so we can show users `Arrêté 1275.pdf` (not
# `.txt`) and deep-link to where the file was originally posted.
_pdf_meta_by_txt: dict[str, dict] = {}
_last_query_time: dict[int, float] = {}

HISTORY_TURNS = 5
HISTORY_TTL_HOURS = 24
USER_COOLDOWN_S = 1.5
LLM_TIMEOUT_S = 60

WELCOME = (
    "Salam 👋  I'm the **ENSIA Impact** assistant.\n\n"
    "I answer questions grounded in everything shared on the ENSIA Impact "
    "Telegram server — chat messages, PDFs, OCR'd images, plus the "
    "official ensia.edu.dz and v2v.ensia.edu.dz pages and every link "
    "students have shared.\n\n"
    "**Things you can ask:**\n"
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
    "**How to use this bot**\n\n"
    "• In a __direct message__: just type your question.\n"
    "• In a __group__: mention me, reply to one of my messages, or use "
    "`/ask <your question>`.\n"
    "• If my answer feels off, retry with `/deep <your question>` — "
    "it's slower (~15 s) but more careful for ambiguous questions.\n"
    "• I remember the last few exchanges so follow-ups like \"and how do "
    "I apply?\" work. Use /reset to clear memory.\n\n"
    "Examples:\n"
    "• What is the CDE at ENSIA?\n"
    "• How do I register a startup under decree 1275?\n"
    "• Comment soumettre un PFE comme projet startup?\n"
    "• هل تنصحني بالدراسة في الخارج؟"
)


# ─── helpers (lifted verbatim from telegram_bot.py where possible) ─────

def _build_topic_id_map() -> dict[int, int]:
    """Map every content msg id → its topic-thread root id, by walking
    reply chains back to a `topic_created` service message. Mirrors the
    logic in the legacy bot."""
    if not MESSAGES_JSON.exists():
        return {}
    try:
        raw = json.loads(MESSAGES_JSON.read_text())
    except Exception:
        return {}
    messages = raw.get("chats", {}).get("list", [{}])[0].get("messages", [])
    topic_ids = {m["id"] for m in messages if m.get("action") == "topic_created"}
    by_id = {m["id"]: m for m in messages if "id" in m}
    out: dict[int, int] = {}
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
                out[m["id"]] = rid
                break
            if rid in visited:
                break
            visited.add(rid)
            cur = by_id.get(rid)
    return out


def _build_pdf_msg_map() -> dict[str, int]:
    """Map original PDF filename → the Telegram message id that posted it."""
    if not MESSAGES_JSON.exists():
        return {}
    try:
        raw = json.loads(MESSAGES_JSON.read_text())
    except Exception:
        return {}
    messages = raw.get("chats", {}).get("list", [{}])[0].get("messages", [])
    out: dict[str, int] = {}
    for m in messages:
        if m.get("type") != "message":
            continue
        file_field = m.get("file")
        if not file_field or not str(file_field).lower().endswith(".pdf"):
            continue
        name = Path(file_field).name
        out[unicodedata.normalize("NFC", name)] = m["id"]
    return out


def _telegram_link(message_id: int) -> str:
    topic_id = _topic_id_by_msg.get(message_id)
    if topic_id:
        return f"https://t.me/c/{ENSIA_CHAT_ID}/{topic_id}/{message_id}"
    return f"https://t.me/c/{ENSIA_CHAT_ID}/{message_id}"


def _msg_id_for_pdf(name: str) -> int | None:
    return _msg_id_by_pdf_name.get(unicodedata.normalize("NFC", name))


def _build_pdf_txt_meta_map() -> dict[str, dict]:
    """Map the chunk's `pdf_file` metadata (a .txt name) → the original
    .pdf filename + the Telegram message id that shared it.

    The chunks store the EXTRACTED txt name (e.g. `Arrêté_1275.txt`)
    produced by `01_extract_pdfs.py`, which NFC-normalises the PDF stem
    and replaces non-word chars with `_`. We replay that transform here
    to derive the same key from `messages_enriched.json`, so citations
    can render the user-friendly original name + a deep link.
    """
    if not MESSAGES_JSON.exists():
        return {}
    try:
        raw = json.loads(MESSAGES_JSON.read_text())
    except Exception:
        return {}
    messages = raw.get("chats", {}).get("list", [{}])[0].get("messages", [])
    out: dict[str, dict] = {}
    for m in messages:
        if m.get("type") != "message":
            continue
        file_field = m.get("file")
        if not file_field or not str(file_field).lower().endswith(".pdf"):
            continue
        pdf_name = Path(file_field).name
        stem = unicodedata.normalize("NFC", Path(pdf_name).stem)
        # SAME transformation 01_extract_pdfs.py uses: \w-allowed, rest → _.
        stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
        txt_key = unicodedata.normalize("NFC", stem + ".txt")
        out[txt_key] = {"original": pdf_name, "message_id": m["id"]}
    return out


def _pdf_meta(txt_name: str) -> dict | None:
    """Look up `{original, message_id}` for a chunk's pdf_file txt name."""
    if not txt_name:
        return None
    return _pdf_meta_by_txt.get(unicodedata.normalize("NFC", txt_name))


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# ─── feedback keyboard ─────────────────────────────────────────────────

# Telethon: callback_data is up to 64 bytes. We tag with "fb:" so other
# button types could coexist later without clashing.
def _feedback_keyboard(refusal: bool = False):
    """Telethon inline keyboard — list of rows of buttons."""
    row = []
    if not refusal:
        row.append(Button.inline("👍 Useful", b"fb:useful"))
    row.append(Button.inline("👎 Not useful", b"fb:not_useful"))
    row.append(Button.inline("🚩 Report", b"fb:report"))
    return [row]


def _keyboard_with_active(active: str):
    def lbl(text: str, key: str) -> str:
        return f"✓ {text}" if key == active else text
    return [[
        Button.inline(lbl("👍 Useful", "useful"), b"fb:useful"),
        Button.inline(lbl("👎 Not useful", "not_useful"), b"fb:not_useful"),
        Button.inline(lbl("🚩 Report", "report"), b"fb:report"),
    ]]


_RATING_LABEL = {
    "useful": "👍 Marked useful",
    "not_useful": "👎 Marked not useful",
    "report": "🚩 Reported — reply with a short reason (or ignore to skip)",
    "updated": "✓ Updated",
    "removed": "↩ Cleared",
}


# ─── reply formatter ───────────────────────────────────────────────────

def _format_reply(result: dict, elapsed_s: float) -> str:
    """Render the answer + sources block in Telethon-flavored markdown.

    Telethon's parse_mode='md' has slight differences vs. python-telegram-
    bot's HTML mode, so we generate plain text + URLs rather than HTML.
    """
    parts = [result["answer"]]

    used = result.get("context_tokens") or 0
    cap = result.get("context_max")
    ctx_bit = ""
    if used > 0 and cap:
        pct = round(used / cap * 100)
        ctx_bit = f" · 🧠 {pct}% ({_fmt_tokens(used)}/{_fmt_tokens(cap)})"
    elif used > 0:
        ctx_bit = f" · 🧠 {_fmt_tokens(used)} tokens"
    timing_line = f"_⏱ {elapsed_s:.1f}s{ctx_bit}_"

    if result.get("refused"):
        parts.append("\n" + timing_line)
        return "\n".join(parts)

    sources = result.get("sources") or []
    if sources:
        parts.append("\n**📚 Sources**")
        for s in sources:
            md = s["metadata"]
            num = s.get("number")
            tag = f"[{num}]" if num is not None else "•"
            kind = md.get("source_type")
            if kind == "chat":
                topic = md.get("topic") or "—"
                date = (md.get("date") or "")[:10]
                msg_id = md.get("message_id")
                link = _telegram_link(msg_id) if isinstance(msg_id, int) else None
                label = topic + (f" · {date}" if date else "")
                if link:
                    label = f"[{label}]({link})"
                parts.append(f"{tag} {label}")
            elif kind == "external":
                url = md.get("url") or ""
                title = md.get("title") or url
                if url:
                    parts.append(f"{tag} 🌐 [{title}]({url})")
                else:
                    parts.append(f"{tag} 🌐 {title}")
            else:  # pdf
                txt_name = md.get("pdf_file") or "?"
                info = _pdf_meta(txt_name)
                if info:
                    # Show the ORIGINAL .pdf name (not the .txt extraction
                    # artifact) and deep-link to the chat message that
                    # posted it.
                    display = info["original"]
                    parts.append(
                        f"{tag} 📄 [{display}]({_telegram_link(info['message_id'])})"
                    )
                else:
                    # Fallback: best-effort prettify by replacing _ with
                    # spaces and showing it as .pdf so the user never
                    # sees `.txt` artifacts.
                    pretty = txt_name.replace("_", " ").rsplit(".", 1)[0] + ".pdf"
                    parts.append(f"{tag} 📄 {pretty}")

    parts.append("\n" + timing_line)
    return "\n".join(parts)


# ─── core handler logic ────────────────────────────────────────────────

async def _handle_query(
    client: TelegramClient, event, query: str, *, rerank: bool = False,
) -> None:
    chat_id = event.chat_id
    sender = await event.get_sender()
    user_id = sender.id if sender else 0
    username = sender.username if sender else None

    # Per-user cooldown.
    now = time.monotonic()
    last = _last_query_time.get(user_id, 0.0)
    if now - last < USER_COOLDOWN_S:
        wait_s = USER_COOLDOWN_S - (now - last)
        await event.reply(f"⏳ Slow down — try again in {wait_s:.0f}s.")
        return
    _last_query_time[user_id] = now

    logger.info("query from @%s in chat %s: %r", username, chat_id, query[:80])

    # ── React + run DB work in parallel ────────────────────────────────
    # The 🤔 reaction is the user-visible "we got your message" signal,
    # and it doesn't depend on anything in Postgres. Earlier code did
    # three sequential DB calls (upsert + counter + history) BEFORE the
    # reaction — when Neon was paused (5-min idle autopause on the free
    # tier) the user could wait 3-4s before seeing 🤔.
    #
    # Fix: fire the reaction first, then run DB work concurrently via
    # `asyncio.gather` so the bot is back on the event loop ASAP. The
    # psycopg calls are sync, so they're shoved into a thread via
    # `asyncio.to_thread` to keep them off the loop.

    t_react = time.monotonic()
    reaction_task = asyncio.create_task(
        _set_thinking_reaction(client, event)
    )

    def _db_work() -> list[dict]:
        if _memory is None:
            return []
        if sender is not None:
            try:
                _memory.upsert_user(
                    user_id=sender.id,
                    username=sender.username,
                    first_name=sender.first_name,
                    last_name=sender.last_name,
                    language_code=getattr(sender, "lang_code", None),
                    is_bot=bool(sender.bot),
                )
                _memory.increment_query_count(sender.id)
            except Exception:
                logger.exception("memory upsert failed")
        try:
            return _memory.recent_turns(
                chat_id, user_id,
                n=HISTORY_TURNS, max_age_hours=HISTORY_TTL_HOURS,
            )
        except Exception:
            logger.exception("memory.recent_turns failed; proceeding without")
            return []

    db_task = asyncio.create_task(asyncio.to_thread(_db_work))

    reaction_set, history = await asyncio.gather(
        reaction_task, db_task, return_exceptions=False,
    )
    logger.info(
        "react+db done in %.2fs (reaction=%s, history=%d)",
        time.monotonic() - t_react, bool(reaction_set), len(history),
    )

    # ── Streaming reply ────────────────────────────────────────────────
    # Send a placeholder immediately so the user has a target message to
    # watch. The LLM call runs in a thread; tokens arrive via a callback
    # that hops back onto the event loop via call_soon_threadsafe and a
    # queue. A throttled editor task drains the queue and edits the
    # placeholder at most once per second so we don't trip Telegram's
    # edit rate-limit. When the LLM finishes we do one final edit with
    # the FORMATTED answer (renumbered citations + sources + footer)
    # and attach the feedback keyboard.
    t_start = time.monotonic()
    loop = asyncio.get_running_loop()
    stream_queue: asyncio.Queue = asyncio.Queue()

    sent_msg = None
    try:
        # 💭 (thought balloon) reads as "thinking" while we wait for the
        # LLM's first token. Replaced with the actual text + a typing
        # cursor by the editor loop once tokens start arriving.
        sent_msg = await event.reply("💭", parse_mode=None, link_preview=False)
    except Exception:
        logger.exception("placeholder send failed")
        await _clear_reaction(client, event, reaction_set)
        await event.reply("Something went wrong on my side. Try again in a moment.")
        return

    async def _edit_loop():
        """Drains the queue, edits the placeholder at most ~1×/s.

        The queue carries the running `full_text`; sentinel None means
        the LLM call finished and the final-format edit is coming next."""
        last_text = ""
        last_edit_at = 0.0
        EDIT_INTERVAL_S = 1.0
        EDIT_CHAR_DELTA = 250
        while True:
            full = await stream_queue.get()
            if full is None:  # sentinel — answer() returned
                return
            # Drain to the latest available chunk so we don't lag.
            while not stream_queue.empty():
                try:
                    nxt = stream_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    return
                full = nxt
            now = time.monotonic()
            if (
                full == last_text
                or (now - last_edit_at < EDIT_INTERVAL_S
                    and len(full) - len(last_text) < EDIT_CHAR_DELTA)
            ):
                continue
            # Truncate defensively to Telegram's 4096-char cap. Strip
            # parse_mode during streaming so half-rendered markdown
            # (open `**` etc.) doesn't 400 on edit.
            display = full[:4000] + " ▌" if len(full) <= 4000 else full[:4000].rstrip() + "…"
            try:
                await sent_msg.edit(display, parse_mode=None, link_preview=False)
                last_text = full
                last_edit_at = now
            except Exception:
                logger.debug("stream edit failed", exc_info=True)

    def _on_chunk(_delta: str, full: str) -> None:
        # Runs in the answer() thread — hop to the asyncio loop.
        try:
            loop.call_soon_threadsafe(stream_queue.put_nowait, full)
        except RuntimeError:
            pass  # loop closed — nothing to do

    edit_task = asyncio.create_task(_edit_loop())
    try:
        async with client.action(chat_id, "typing"):
            result = await asyncio.to_thread(
                answer, query, retriever=_retriever, history=history,
                rerank=rerank, stream_callback=_on_chunk,
            )
    except Exception:
        logger.exception("answer() failed")
        await stream_queue.put(None)  # let edit loop exit cleanly
        await edit_task
        await _clear_reaction(client, event, reaction_set)
        try:
            await sent_msg.edit("Something went wrong on my side. Try again in a moment.")
        except Exception:
            await event.reply("Something went wrong on my side. Try again in a moment.")
        return

    # Tell the edit loop we're done; let it drain.
    await stream_queue.put(None)
    await edit_task

    elapsed_s = time.monotonic() - t_start
    timings = result.get("timings") or {}

    logger.info(
        "answered total=%.1fs · rewrite=%.1fs · retrieval=%.1fs · answer=%.1fs · tier=%s · score=%.3f",
        elapsed_s,
        timings.get("rewrite", 0.0),
        timings.get("retrieval", 0.0),
        timings.get("answer", 0.0),
        result["tier"],
        result["top_score"],
    )

    reply_text = _format_reply(result, elapsed_s)
    if len(reply_text) > 4000:
        reply_text = reply_text[:4000].rstrip() + "…"

    keyboard = _feedback_keyboard(refusal=bool(result.get("refused")))
    try:
        await sent_msg.edit(
            reply_text,
            parse_mode="md",
            buttons=keyboard,
            link_preview=False,
        )
    except Exception:
        logger.exception("final markdown edit failed; retrying as plain")
        try:
            await sent_msg.edit(reply_text, buttons=keyboard, link_preview=False)
        except Exception:
            logger.exception("final edit failed entirely")

    await _clear_reaction(client, event, reaction_set)

    # Persist on success only.
    if _memory is not None and not result.get("refused"):
        try:
            _memory.add_turns(chat_id, user_id, [
                {"role": "user", "content": query, "rerank": rerank},
                {
                    "role": "assistant",
                    "content": result["answer"],
                    "sources": result.get("sources") or None,
                    "tg_message_id": sent_msg.id if sent_msg else None,
                    "model_used": result.get("model_used"),
                },
            ])
        except Exception:
            logger.exception("memory.add_turns failed")

        # Gamification: streak bump + check for newly earned badges.
        # All best-effort — never block the user on this.
        try:
            await asyncio.to_thread(_memory.update_streak, user_id)
            stats = await asyncio.to_thread(_memory.get_user_stats, user_id)
            if stats:
                from chatbot.gamification import newly_earned, BADGES
                fresh = newly_earned(stats, stats.get("badges") or [])
                if fresh:
                    new_keys = (stats.get("badges") or []) + [b.key for b in fresh]
                    await asyncio.to_thread(_memory.set_badges, user_id, new_keys)
                    # Send a small congratulatory note. Multiple badges
                    # combined onto one message to keep it un-spammy.
                    lines = [f"{b.emoji} **{b.name}** — _{b.description}_" for b in fresh]
                    await event.reply(
                        "🎉 New badge"
                        + ("s" if len(fresh) > 1 else "")
                        + " unlocked!\n\n" + "\n".join(lines)
                        + "\n\nSee all your badges with /me.",
                        parse_mode="md",
                        link_preview=False,
                    )
        except Exception:
            logger.exception("gamification update failed")


async def _set_thinking_reaction(client, event) -> bool:
    """Best-effort 🤔 on the user's message. Returns True if set, False
    on any failure (very old chats can disallow bot reactions)."""
    try:
        await client(SendReactionRequest(
            peer=await event.get_input_chat(),
            msg_id=event.message.id,
            reaction=[ReactionEmoji(emoticon="🤔")],
        ))
        return True
    except Exception:
        logger.debug("reaction failed", exc_info=True)
        return False


async def _clear_reaction(client, event, was_set: bool):
    if not was_set:
        return
    try:
        await client(SendReactionRequest(
            peer=await event.get_input_chat(),
            msg_id=event.message.id,
            reaction=[],
        ))
    except Exception:
        logger.debug("clear reaction failed", exc_info=True)


# ─── main ───────────────────────────────────────────────────────────────

async def _setup_client() -> TelegramClient:
    """Connect via MTProto using the bot token. Telethon stores a small
    session SQLite alongside (data/.telethon_bot.session) — different
    file from the user-account session used by 00_telegram_sync.py."""
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]

    session_path = ROOT / "data" / ".telethon_bot.session"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_path.with_suffix("")),
        api_id, api_hash,
        connection_retries=10,
        retry_delay=2,
        request_retries=5,
    )
    await client.start(bot_token=bot_token)
    me = await client.get_me()
    global _bot_username
    _bot_username = me.username
    logger.info("Bot identity: @%s (id=%d)", me.username, me.id)

    # Register the bot's commands so Telegram clients render a "Menu"
    # button next to the input field listing them. Setting at the
    # "default" scope makes them visible everywhere (DMs + groups).
    # Telegram caps descriptions at 256 chars; keep them under ~64 for
    # the inline menu to read cleanly.
    commands = [
        BotCommand(command="start", description="Welcome message + example questions"),
        BotCommand(command="help", description="How to use the bot"),
        BotCommand(command="ask", description="Ask a question (works in groups)"),
        BotCommand(command="deep", description="Slow but more careful answer for ambiguous questions"),
        BotCommand(command="reset", description="Clear my memory of this conversation"),
        BotCommand(command="me", description="My stats: questions, streak, badges"),
    ]
    try:
        await client(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=commands,
        ))
        logger.info("Registered %d bot commands with Telegram", len(commands))
    except Exception:
        logger.exception("SetBotCommandsRequest failed (non-fatal)")
    return client


async def _run() -> None:
    global _retriever, _memory, _topic_id_by_msg, _msg_id_by_pdf_name, _pdf_meta_by_txt

    logger.info("Building topic-link map…")
    _topic_id_by_msg = _build_topic_id_map()
    logger.info("Topic map: %d", len(_topic_id_by_msg))

    _msg_id_by_pdf_name = _build_pdf_msg_map()
    logger.info("PDF→msg map: %d PDFs linkable", len(_msg_id_by_pdf_name))

    _pdf_meta_by_txt = _build_pdf_txt_meta_map()
    logger.info("PDF txt→meta map: %d entries", len(_pdf_meta_by_txt))

    _memory = ConversationMemory()
    safe_dsn = re.sub(r":[^:@/]+@", ":***@", _memory.dsn)
    logger.info("Conversation memory ready (postgres %s)", safe_dsn)

    logger.info("Loading retriever (BGE-M3 + reranker)…")
    _retriever = Retriever()
    _retriever.search("hello", k=1, rerank=True)
    logger.info("Retriever ready")

    client = await _setup_client()

    # ── /start ────────────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def on_start(event):
        await event.reply(WELCOME, parse_mode="md", link_preview=False)

    # ── /help ─────────────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
    async def on_help(event):
        await event.reply(HELP, parse_mode="md", link_preview=False)

    # ── /reset ────────────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"^/reset(?:@\w+)?$"))
    async def on_reset(event):
        if _memory is None:
            return
        sender = await event.get_sender()
        if sender is None:
            return
        try:
            n = _memory.reset(event.chat_id, sender.id)
            await event.reply(
                f"🧹 Cleared {n} message{'s' if n != 1 else ''} from memory."
                if n
                else "Nothing to clear — no previous conversation found."
            )
        except Exception:
            logger.exception("reset failed")

    # ── /ask <query> ──────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"^/ask(?:@\w+)?\s+(.+)$"))
    async def on_ask(event):
        m = re.match(r"^/ask(?:@\w+)?\s+(.+)$", event.raw_text, re.DOTALL)
        if not m:
            return
        await _handle_query(client, event, m.group(1).strip())

    # ── /deep <query> — opt-in slow + accurate path ──────────────────
    # Runs the same pipeline but with rerank=True. ~10-15x slower on
    # cpu-basic; useful for ambiguous questions where the default
    # dense-only retrieval misses the right chunk.
    @client.on(events.NewMessage(pattern=r"^/deep(?:@\w+)?\s+(.+)$"))
    async def on_deep(event):
        m = re.match(r"^/deep(?:@\w+)?\s+(.+)$", event.raw_text, re.DOTALL)
        if not m:
            return
        await _handle_query(client, event, m.group(1).strip(), rerank=True)

    # ── /me — personal stats card ─────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"^/me(?:@\w+)?$"))
    async def on_me(event):
        if _memory is None:
            await event.reply("Stats unavailable right now.")
            return
        sender = await event.get_sender()
        if sender is None:
            return
        try:
            stats = await asyncio.to_thread(_memory.get_user_stats, sender.id)
        except Exception:
            logger.exception("get_user_stats failed")
            await event.reply("Couldn't fetch your stats — try again in a moment.")
            return
        if not stats:
            await event.reply(
                "I don't have any record of you yet — ask me a question first!"
            )
            return
        from chatbot.gamification import evaluate_badges, format_me_card
        earned = evaluate_badges(stats)
        # Backfill in case the user has badges we never persisted (e.g.
        # they qualified before this code shipped). Don't notify on
        # backfill — they probably already saw the data.
        if [b.key for b in earned] != (stats.get("badges") or []):
            try:
                await asyncio.to_thread(
                    _memory.set_badges, sender.id, [b.key for b in earned]
                )
            except Exception:
                logger.exception("set_badges (backfill) failed")
        text = format_me_card(stats, earned)
        await event.reply(text, parse_mode="md", link_preview=False)

    # ── plain text in DM, or @mention / reply in groups ───────────────
    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if event.raw_text.startswith("/"):
            return  # already handled by command handlers

        text = (event.raw_text or "").strip()
        if not text:
            return

        is_dm = event.is_private
        if not is_dm:
            # Group: respond only if @mentioned or reply to bot.
            addressed = False
            if _bot_username and f"@{_bot_username}" in text:
                addressed = True
                text = text.replace(f"@{_bot_username}", "").strip()
            if event.is_reply:
                replied = await event.get_reply_message()
                if replied and replied.sender_id and replied.out:
                    addressed = True
            if not addressed:
                return
        if not text:
            return

        # Pending-report flow: if the user previously tapped 🚩 Report and
        # hasn't given a reason yet, treat this message as the reason.
        if _memory is not None:
            sender = await event.get_sender()
            if sender:
                try:
                    pending = await asyncio.to_thread(
                        _memory.pending_report, event.chat_id, sender.id
                    )
                except Exception:
                    logger.exception("pending_report check failed")
                    pending = None
                if pending is not None:
                    feedback_id, _msg = pending
                    try:
                        await asyncio.to_thread(
                            _memory.add_report_reason, feedback_id, text[:500]
                        )
                        await event.reply("🙏 Thanks — your report has been saved.")
                        return
                    except Exception:
                        logger.exception("add_report_reason failed")
                        # Fall through; treat as normal query.

        await _handle_query(client, event, text)

    # ── feedback callback ─────────────────────────────────────────────
    @client.on(events.CallbackQuery(pattern=rb"^fb:"))
    async def on_feedback(event):
        if _memory is None:
            await event.answer("Feedback storage unavailable")
            return
        rating = event.data.decode().split(":", 1)[1]
        if rating not in ("useful", "not_useful", "report"):
            await event.answer("Unknown feedback type")
            return
        try:
            outcome = await asyncio.to_thread(
                _memory.set_feedback,
                event.chat_id,
                event.message_id,
                event.sender_id,
                rating,
            )
        except Exception:
            logger.exception("set_feedback failed")
            await event.answer("Couldn't record that — try again")
            return

        label = _RATING_LABEL.get(outcome) or _RATING_LABEL.get(rating, "✓")
        await event.answer(label)

        # Refresh the keyboard to show what's checked.
        try:
            if outcome == "removed":
                await event.edit(buttons=_feedback_keyboard(refusal=False))
            else:
                await event.edit(buttons=_keyboard_with_active(rating))
        except Exception:
            logger.debug("edit reply markup failed", exc_info=True)

        logger.info(
            "feedback %s by user %d on (chat=%d msg=%d): outcome=%s",
            rating, event.sender_id, event.chat_id, event.message_id, outcome,
        )

    logger.info("Bot ready, listening for updates…")
    await client.run_until_disconnected()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
