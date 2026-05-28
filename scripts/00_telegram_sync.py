"""Pull new messages from the Telegram group via MTProto (Telethon).

The Bot API can't read full topic history of a supergroup, so we use a
user-account session instead. Credentials come from `.env`:

    TELEGRAM_API_ID, TELEGRAM_API_HASH   -- https://my.telegram.org/apps
    TELEGRAM_PHONE                       -- the account's phone, e.g. +213…
    TELEGRAM_GROUP                       -- @username or numeric chat id;
                                            blank to use data/result.json

First run logs in interactively (Telegram sends a code to the account's
app — paste it into the terminal). The session is cached at
`data/.telethon.session` and reused silently thereafter. Add that file
to backups but NEVER commit it — it grants full read access to every
chat the account is in.

The script is incremental: it finds the max message id already present
in `data/result.json` and only pulls messages newer than that. The
output JSON keeps the exact same shape as a Telegram Desktop export, so
the rest of the pipeline (`01_extract_pdfs.py` … `07_status_snapshot.py`)
continues to work unchanged.

Media handling: photos and PDFs are NOT downloaded by default — the
script prints a summary of how many were skipped. Pass `--media` to
fetch them into `data/chats/photos/` and `data/chats/files/` so
`02_ocr_images.py` and `01_extract_pdfs.py` pick them up.

Usage:
    .venv/bin/python scripts/00_telegram_sync.py
    .venv/bin/python scripts/00_telegram_sync.py --media
    .venv/bin/python scripts/00_telegram_sync.py --limit 50   # cap for testing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl import types as tlt

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RESULT = DATA / "result.json"
SESSION = DATA / ".telethon.session"
PHOTOS_DIR = DATA / "chats" / "photos"
FILES_DIR = DATA / "chats" / "files"


# ---------------------------------------------------------------------------
# Telegram-Desktop-export serialization
#
# We mirror exactly what the official Telegram Desktop client produces in
# result.json. That format is the ground truth the rest of the pipeline
# already understands: polymorphic `text` field, `text_entities`, service
# actions, photo / file paths, reply_to_message_id pointing at topic
# roots for messages posted inside a forum topic, etc.

ACTION_NAMES = {
    tlt.MessageActionChatCreate: "create_group",
    tlt.MessageActionChatEditTitle: "edit_group_title",
    tlt.MessageActionChatEditPhoto: "edit_group_photo",
    tlt.MessageActionChatDeletePhoto: "delete_group_photo",
    tlt.MessageActionChatAddUser: "invite_members",
    tlt.MessageActionChatJoinedByLink: "join_group_by_link",
    tlt.MessageActionChatDeleteUser: "remove_members",
    tlt.MessageActionPinMessage: "pin_message",
    tlt.MessageActionChannelMigrateFrom: "migrate_from_group",
    tlt.MessageActionChatMigrateTo: "migrate_to_supergroup",
    tlt.MessageActionTopicCreate: "topic_created",
    tlt.MessageActionTopicEdit: "topic_edit",
}

# Telethon entity types we map to Telegram-Desktop's "text_entities" type names.
ENTITY_TYPE_NAMES = {
    tlt.MessageEntityBold: "bold",
    tlt.MessageEntityItalic: "italic",
    tlt.MessageEntityUnderline: "underline",
    tlt.MessageEntityStrike: "strikethrough",
    tlt.MessageEntityCode: "code",
    tlt.MessageEntityPre: "pre",
    tlt.MessageEntityBlockquote: "blockquote",
    tlt.MessageEntityUrl: "link",
    tlt.MessageEntityTextUrl: "text_link",
    tlt.MessageEntityMention: "mention",
    tlt.MessageEntityMentionName: "mention_name",
    tlt.MessageEntityHashtag: "hashtag",
    tlt.MessageEntityCashtag: "cashtag",
    tlt.MessageEntityBotCommand: "bot_command",
    tlt.MessageEntityEmail: "email",
    tlt.MessageEntityPhone: "phone",
    tlt.MessageEntitySpoiler: "spoiler",
    tlt.MessageEntityCustomEmoji: "custom_emoji",
}


def _iso(dt) -> str:
    """2024-09-21T13:47:30 (no timezone, matches export shape)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _from_user(name_cache: dict, peer) -> tuple[str | None, str | None]:
    """Resolve a peer to (display name, "user<id>" string). Falls back to
    raw ids when we don't have the entity in the cache (rare — Telethon
    caches contacts as it sees them)."""
    if peer is None:
        return None, None
    uid = getattr(peer, "user_id", None) or getattr(peer, "channel_id", None)
    if uid is None:
        return None, None
    name = name_cache.get(uid)
    prefix = "user" if getattr(peer, "user_id", None) else "channel"
    return name, f"{prefix}{uid}"


def _build_entities(text: str | None, entities) -> list[dict]:
    """Convert Telethon MessageEntity list to the export's text_entities
    list (a flat list of plain + styled spans covering the whole text)."""
    if not text:
        return []
    if not entities:
        return [{"type": "plain", "text": text}]

    # Build a sorted, non-overlapping coverage of the string. Telegram
    # text_entities is a flat list of segments where adjacent plain text
    # gets its own entry. We approximate by emitting plain gaps between
    # styled entities — overlapping styles get the inner-most type.
    out: list[dict] = []
    cursor = 0
    sorted_ents = sorted(entities, key=lambda e: (e.offset, -e.length))
    for ent in sorted_ents:
        start, end = ent.offset, ent.offset + ent.length
        if start < cursor:  # skip overlaps for simplicity
            continue
        if start > cursor:
            out.append({"type": "plain", "text": text[cursor:start]})
        kind = ENTITY_TYPE_NAMES.get(type(ent), "plain")
        out.append({"type": kind, "text": text[start:end]})
        cursor = end
    if cursor < len(text):
        out.append({"type": "plain", "text": text[cursor:]})
    return out


def _media_paths(msg, want_media: bool) -> dict[str, str]:
    """Return media fields the export format uses (photo, file, mime_type).
    When `want_media` is False we still record what's attached so future
    runs can backfill — the rest of the pipeline only cares about photos
    + PDFs anyway."""
    fields: dict[str, str] = {}
    if isinstance(msg.media, tlt.MessageMediaPhoto):
        fields["photo"] = f"photos/photo_{msg.id}@{_iso(msg.date)}.jpg"
        fields["width"] = 0
        fields["height"] = 0
    elif isinstance(msg.media, tlt.MessageMediaDocument):
        doc = msg.media.document
        if not isinstance(doc, tlt.Document):
            return fields
        name = None
        for attr in doc.attributes:
            if isinstance(attr, tlt.DocumentAttributeFilename):
                name = attr.file_name
                break
        if name:
            fields["file"] = f"files/{name}"
            fields["file_name"] = name
        fields["mime_type"] = doc.mime_type or ""
    return fields


async def _maybe_download(client: TelegramClient, msg, fields: dict) -> None:
    """When --media is on, save the actual bytes so the rest of the
    pipeline can OCR / extract them. Skip if file already exists."""
    target: Path | None = None
    if "photo" in fields:
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        target = DATA / "chats" / fields["photo"]
    elif "file" in fields:
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        target = DATA / "chats" / fields["file"]
    if target is None or target.exists():
        return
    print(f"    ↓ {target.relative_to(DATA)}")
    await client.download_media(msg, file=str(target))


def _serialize_service(msg, name_cache: dict) -> dict | None:
    act = msg.action
    name = ACTION_NAMES.get(type(act))
    if name is None:
        return None  # action types we don't model — skip silently

    actor, actor_id = _from_user(name_cache, msg.from_id or msg.peer_id)
    out: dict[str, Any] = {
        "id": msg.id,
        "type": "service",
        "date": _iso(msg.date),
        "date_unixtime": str(int(msg.date.timestamp())),
        "actor": actor,
        "actor_id": actor_id,
        "action": name,
        "text": "",
        "text_entities": [],
    }
    if isinstance(act, tlt.MessageActionTopicCreate):
        out["title"] = act.title
    elif isinstance(act, tlt.MessageActionChatEditTitle):
        out["title"] = act.title
    elif isinstance(act, tlt.MessageActionChatCreate):
        out["title"] = act.title
        out["members"] = [name_cache.get(uid) for uid in act.users if name_cache.get(uid)]
    elif isinstance(act, tlt.MessageActionChatAddUser):
        out["members"] = [name_cache.get(uid) for uid in act.users if name_cache.get(uid)]
    elif isinstance(act, tlt.MessageActionChatDeleteUser):
        out["members"] = [name_cache.get(act.user_id)] if name_cache.get(act.user_id) else []
    return out


async def _serialize_message(
    client: TelegramClient,
    msg,
    name_cache: dict,
    want_media: bool,
) -> dict | None:
    if isinstance(msg, tlt.MessageService):
        return _serialize_service(msg, name_cache)
    if not isinstance(msg, tlt.Message):
        return None

    sender, sender_id = _from_user(name_cache, msg.from_id or msg.peer_id)
    text = msg.message or ""
    out: dict[str, Any] = {
        "id": msg.id,
        "type": "message",
        "date": _iso(msg.date),
        "date_unixtime": str(int(msg.date.timestamp())),
        "from": sender,
        "from_id": sender_id,
        "text": text,
        "text_entities": _build_entities(text, msg.entities),
    }
    # Forum topic membership: when posting inside a topic, Telegram sets
    # reply_to_top_id = topic_root_msg_id. Top-level topic messages get
    # reply_to_message_id == topic_root (matching the Desktop export).
    if msg.reply_to:
        rt = msg.reply_to
        if isinstance(rt, tlt.MessageReplyHeader):
            out["reply_to_message_id"] = rt.reply_to_msg_id or rt.reply_to_top_id

    if msg.media is not None:
        fields = _media_paths(msg, want_media)
        out.update(fields)
        if want_media and fields:
            await _maybe_download(client, msg, fields)

    return out


# ---------------------------------------------------------------------------
# Main sync routine

async def _populate_name_cache(client: TelegramClient, chat) -> dict[int, str]:
    """Map user_id → display name for every participant we can see. The
    export format wants "Wahid Chami ENSIA"-style names, not @usernames."""
    cache: dict[int, str] = {}
    try:
        async for p in client.iter_participants(chat, limit=None):
            full = " ".join(filter(None, [p.first_name, p.last_name])) or (
                f"@{p.username}" if p.username else f"user{p.id}"
            )
            cache[p.id] = full
    except Exception as e:
        print(f"  ⚠ couldn't list participants ({e}); names may be missing")
    return cache


def _resolve_chat_target(payload: dict | None) -> str | int:
    """TELEGRAM_GROUP wins; otherwise pull the chat id out of the existing
    export. Supergroup ids in Telethon are -100<id>."""
    target = os.environ.get("TELEGRAM_GROUP", "").strip()
    if target:
        # numeric chat id passed as positive form? convert
        if target.lstrip("-").isdigit():
            n = int(target)
            return n if n < 0 else -1000000000000 - n
        return target
    if payload and payload.get("chats", {}).get("list"):
        cid = payload["chats"]["list"][0]["id"]
        return -1000000000000 - int(cid)
    raise SystemExit(
        "Could not determine which chat to sync. Set TELEGRAM_GROUP in .env "
        "or run an initial Telegram Desktop export so data/result.json exists."
    )


async def sync(limit: int | None, want_media: bool) -> None:
    load_dotenv(ROOT / ".env")
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    phone = os.environ.get("TELEGRAM_PHONE")
    if not (api_id and api_hash and phone):
        raise SystemExit(
            "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE in .env"
        )

    payload = json.loads(RESULT.read_text()) if RESULT.exists() else None
    target = _resolve_chat_target(payload)

    # max_id: largest *content* message id already on disk (service msgs
    # use synthetic negative ids in exports, hence the filter).
    if payload:
        existing = payload["chats"]["list"][0]["messages"]
        max_id = max((m["id"] for m in existing if m["id"] > 0), default=0)
    else:
        existing = []
        max_id = 0
    print(f"Local export: {len(existing)} msgs, max id {max_id}")
    print(f"Target chat:  {target}")

    SESSION.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(SESSION.with_suffix("")), int(api_id), api_hash)
    await client.start(phone=phone)

    chat = await client.get_entity(target)
    chat_id = abs(chat.id) if hasattr(chat, "id") else None
    chat_title = getattr(chat, "title", "?")
    print(f"Resolved:     {chat_title} (id {chat_id})")

    name_cache = await _populate_name_cache(client, chat)
    print(f"Participants: {len(name_cache)} known names")

    new_msgs: list[dict] = []
    skipped_media = {"photos": 0, "files": 0}
    print("Fetching new messages…")
    async for msg in client.iter_messages(
        chat, min_id=max_id, reverse=True, limit=limit
    ):
        rec = await _serialize_message(client, msg, name_cache, want_media)
        if rec is None:
            continue
        new_msgs.append(rec)
        if not want_media:
            if "photo" in rec:
                skipped_media["photos"] += 1
            elif "file" in rec:
                skipped_media["files"] += 1
        if len(new_msgs) % 50 == 0:
            print(f"  fetched {len(new_msgs)}…")

    await client.disconnect()

    if not new_msgs:
        print("✓ Already up to date.")
        return

    print(f"Fetched {len(new_msgs)} new messages.")
    if not want_media and (skipped_media["photos"] or skipped_media["files"]):
        print(
            f"  ↪ skipped {skipped_media['photos']} photo(s) + "
            f"{skipped_media['files']} file(s) (pass --media to download)"
        )

    # Merge & write back. We trust the API's id ordering — no in-place
    # dedup needed because min_id excludes everything ≤ max_id.
    if payload is None:
        payload = {
            "about": "",
            "chats": {
                "list": [{
                    "name": chat_title,
                    "type": "private_supergroup",
                    "id": chat_id,
                    "messages": [],
                }]
            },
        }
    payload["chats"]["list"][0]["messages"].extend(new_msgs)

    # indent=1 mirrors the Telegram Desktop export (keeps diffs minimal).
    RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"✓ Wrote {RESULT.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--media",
        action="store_true",
        help="Download photos + files into data/chats/ so OCR / PDF "
        "stages can process them. Off by default — text-only sync is "
        "much faster and matches what the existing pipeline expects.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of new messages fetched (for smoke tests).",
    )
    args = ap.parse_args()
    try:
        asyncio.run(sync(limit=args.limit, want_media=args.media))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
