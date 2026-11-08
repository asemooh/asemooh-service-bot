import os
import re
import json
import time
import html
import sqlite3
import logging
from datetime import datetime, timezone
from collections import defaultdict, deque
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, ContextTypes, filters,
)

# Asemooh Service Bot - consolidated group management bot
# Keep BOT_TOKEN in Railway Variables. Never put the token in this file.
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "asemooh.sqlite3")
FLOOD_WINDOW_SECONDS = 8
FLOOD_LIMIT = 6
WARNING_LIMIT = 3
BAD_WORDS = [w.strip().lower() for w in os.getenv("BAD_WORDS", "").split(",") if w.strip()]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("asemooh")

DEFAULT_SETTINGS = {
    "welcome": True,
    "links": True,
    "antispam": True,
    "badwords": bool(BAD_WORDS),
    "warnings": True,
    "media_block": False,
    "global_silence": False,
}
ROLE_LABELS = {
    "admin": "👮 Admin",
    "cofounder": "⚜ Co-Founder",
    "moderator": "👮 Moderator",
    "cleaner": "🛃 Chat Cleaner",
    "muter": "🙊 Muter",
    "helper": "⛑ Helper",
    "free": "🔓 Free",
}
ROLE_COMMANDS = {
    "cofounder": ("cofounder", "uncofounder"),
    "moderator": ("mod", "unmod"),
    "cleaner": ("cleaner", "uncleaner"),
    "muter": ("muter", "unmuter"),
    "helper": ("helper", "unhelper"),
    "free": ("free", "unfree"),
}

# ---------- SQLite storage ----------

def db():
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roles (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            role TEXT NOT NULL, assigned_by INTEGER, assigned_at INTEGER,
            PRIMARY KEY(chat_id, user_id, role)
        );
        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS activity (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            username TEXT, first_name TEXT, last_seen INTEGER NOT NULL,
            messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS links (
            chat_id INTEGER PRIMARY KEY, staff_chat_id INTEGER, log_chat_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS custom_welcome (
            chat_id INTEGER PRIMARY KEY, message TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS custom_replies (
            chat_id INTEGER NOT NULL, trigger TEXT NOT NULL, response TEXT NOT NULL,
            PRIMARY KEY(chat_id, trigger)
        );
        CREATE TABLE IF NOT EXISTS bot_messages (
            chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL
        );
        """)


def get_settings(chat_id: int) -> dict:
    with db() as con:
        row = con.execute("SELECT data FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    values = DEFAULT_SETTINGS.copy()
    if row:
        try:
            values.update(json.loads(row["data"]))
        except (ValueError, TypeError):
            log.exception("Invalid settings JSON for %s", chat_id)
    return values


def save_settings(chat_id: int, values: dict):
    with db() as con:
        con.execute("INSERT INTO settings(chat_id,data) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data",
                    (chat_id, json.dumps(values)))


def get_role(chat_id: int, user_id: int) -> Optional[str]:
    # Co-Founder and all lower roles are overlays; Telegram creator/admin status is checked separately.
    with db() as con:
        row = con.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=? ORDER BY CASE role WHEN 'cofounder' THEN 0 ELSE 1 END LIMIT 1",
                           (chat_id, user_id)).fetchone()
    return row["role"] if row else None


def get_all_roles(chat_id: int, user_id: int) -> list:
    with db() as con:
        rows = con.execute("SELECT role FROM roles WHERE chat_id=? AND user_id=? ORDER BY role", (chat_id, user_id)).fetchall()
    return [r["role"] for r in rows]


def assign_role(chat_id: int, user_id: int, role: str, actor_id: int):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO roles(chat_id,user_id,role,assigned_by,assigned_at) VALUES(?,?,?,?,?)",
                    (chat_id, user_id, role, actor_id, int(time.time())))
        # The following roles imply Free protection in the Charleyservice model.
        if role in ("moderator", "muter"):
            con.execute("INSERT OR IGNORE INTO roles(chat_id,user_id,role,assigned_by,assigned_at) VALUES(?,?,?,?,?)",
                        (chat_id, user_id, "free", actor_id, int(time.time())))


def remove_role(chat_id: int, user_id: int, role: str):
    with db() as con:
        con.execute("DELETE FROM roles WHERE chat_id=? AND user_id=? AND role=?", (chat_id, user_id, role))
        if role in ("moderator", "muter"):
            # Keep Free if another role independently needs it only when directly assigned is not distinguishable.
            con.execute("DELETE FROM roles WHERE chat_id=? AND user_id=? AND role='free'", (chat_id, user_id))


def get_link_ids(chat_id: int):
    with db() as con:
        row = con.execute("SELECT staff_chat_id,log_chat_id FROM links WHERE chat_id=?", (chat_id,)).fetchone()
    return (row["staff_chat_id"], row["log_chat_id"]) if row else (None, None)


def set_link(chat_id: int, field: str, value: Optional[int]):
    if field not in ("staff_chat_id", "log_chat_id"):
        raise ValueError("Invalid link field")
    with db() as con:
        con.execute("INSERT OR IGNORE INTO links(chat_id) VALUES(?)", (chat_id,))
        con.execute(f"UPDATE links SET {field}=? WHERE chat_id=?", (value, chat_id))


# ---------- common helpers ----------

async def reply(update: Update, text: str, **kwargs):
    message = update.effective_message
    if message:
        await message.reply_text(text, **kwargs)


async def telegram_status(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status


async def is_creator(chat_id: int, user_id: int, context) -> bool:
    return await telegram_status(chat_id, user_id, context) == "creator"


async def is_telegram_admin(chat_id: int, user_id: int, context) -> bool:
    return await telegram_status(chat_id, user_id, context) in ("administrator", "creator")


async def can_manage_roles(update: Update, context) -> bool:
    chat = update.effective_chat
    actor = update.effective_user
    if not chat or not actor or chat.type == ChatType.PRIVATE:
        await reply(update, "Use this command inside your group.")
        return False
    status = await telegram_status(chat.id, actor.id, context)
    role = get_role(chat.id, actor.id)
    if status == "creator" or (status == "administrator" and role == "cofounder"):
        return True
    await reply(update, "⛔ Only the group Founder or a Co-Founder can manage bot roles.")
    return False


async def has_permission(chat_id: int, user_id: int, action: str, context) -> bool:
    """Telegram rights are always required from the bot; role overlays limit staff actions."""
    status = await telegram_status(chat_id, user_id, context)
    roles = set(get_all_roles(chat_id, user_id))
    if status == "creator":
        return True
    if "cofounder" in roles:
        return status == "administrator"
    if status == "administrator":
        return True
    if action == "moderate" and "moderator" in roles:
        return True
    if action == "mute" and ("moderator" in roles or "muter" in roles):
        return True
    if action == "delete" and "cleaner" in roles:
        return True
    return False


async def ensure_action(update: Update, context, action: str) -> bool:
    chat = update.effective_chat
    actor = update.effective_user
    if not chat or chat.type == ChatType.PRIVATE:
        await reply(update, "This command must be used inside a group.")
        return False
    try:
        allowed = await has_permission(chat.id, actor.id, action, context)
    except TelegramError:
        allowed = False
    if not allowed:
        await reply(update, "⛔ You do not have permission to use this command.")
        return False
    return True


async def bot_can(update: Update, context, permission: str) -> bool:
    chat = update.effective_chat
    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if me.status == "creator":
            return True
        return bool(getattr(me, permission, False))
    except TelegramError:
        return False


def replied_user(update: Update):
    m = update.effective_message
    if m and m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user
    return None


async def target_from_command(update: Update, context):
    user = replied_user(update)
    if user:
        return user
    if context.args:
        raw = context.args[0]
        if raw.isdigit():
            try:
                return await context.bot.get_chat(int(raw))
            except TelegramError:
                return None
    return None


def group_only(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type != ChatType.PRIVATE)


# ---------- menus and settings ----------

def main_menu(bot_username: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add me to a group", url=f"https://t.me/{bot_username}?startgroup=true")],
        [InlineKeyboardButton("👥 Group", callback_data="page:group"), InlineKeyboardButton("📢 Channel", callback_data="page:channel")],
        [InlineKeyboardButton("🆘 Support", callback_data="page:support"), InlineKeyboardButton("ℹ️ Information", callback_data="page:info")],
        [InlineKeyboardButton("🌍 Languages", callback_data="page:languages"), InlineKeyboardButton("🔒 Privacy", callback_data="page:privacy")],
    ])


def settings_keyboard(chat_id: int):
    values = get_settings(chat_id)
    labels = {
        "welcome": "👋 Welcome messages", "links": "🔗 Link protection",
        "antispam": "🛡 Anti-flood", "badwords": "🚫 Bad words",
        "warnings": "⚠️ Warning system", "media_block": "🖼 Media block",
        "global_silence": "🔇 Global silence",
    }
    rows = [[InlineKeyboardButton(f"{label}: {'ON 🟢' if values[key] else 'OFF 🔴'}", callback_data=f"setting:{key}")]
            for key, label in labels.items()]
    rows.append([InlineKeyboardButton("📜 Rules", callback_data="show:rules")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = await context.bot.get_me()
    await reply(update,
        "👋 Hello!\n\n🤖 Welcome to Asemooh Service Bot.\n"
        "Add me to your group and promote me as an administrator.\n\n"
        "Use /help to see commands.", reply_markup=main_menu(info.username))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 ASEMOOH SERVICE BOT — COMMANDS\n\n"
        "📌 GENERAL\n/start /help /rules /settings /staff /info /infopvt /me\n\n"
        "🛡 MEMBER MODERATION\n/ban /unban /kick /mute /unmute /warn /unwarn /warns /delwarn\n\n"
        "🧹 MESSAGE TOOLS\n/del /logdel /geturl /pin /editpin /delpin /repin /pinned\n\n"
        "👥 ROLES (reply to a user's message)\n/cofounder /uncofounder /mod /unmod\n"
        "/cleaner /uncleaner /muter /unmuter /helper /unhelper /free /unfree\n\n"
        "📊 EXPERT TOOLS\n/list /inactives /graphic /trend /intervention /send\n\n"
        "⚙️ CONFIGURATION\n/setwelcome /resetwelcome /setlog /staffgroup /unstaffgroup\n"
        "/setreply /delreply /reload\n\n"
        "Reply to a member's message for moderation and role commands."
    )
    await reply(update, text)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not group_only(update):
        await reply(update, "Open /settings inside your group.")
        return
    if not await ensure_action(update, context, "settings"):
        return
    await reply(update, "⚙️ GROUP SETTINGS — tap a switch to change it.", reply_markup=settings_keyboard(update.effective_chat.id))


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_panel(update, context)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id if q.message else None
    if data.startswith("setting:"):
        if not chat_id:
            return
        try:
            if not await is_telegram_admin(chat_id, q.from_user.id, context):
                await q.answer("Telegram administrators only", show_alert=True)
                return
        except TelegramError:
            await q.answer("Could not verify your admin status", show_alert=True)
            return
        key = data.split(":", 1)[1]
        values = get_settings(chat_id)
        if key not in values:
            return
        values[key] = not values[key]
        save_settings(chat_id, values)
        await q.edit_message_reply_markup(reply_markup=settings_keyboard(chat_id))
        return
    if data == "show:rules":
        await q.edit_message_text(RULES)
    elif data.startswith("page:"):
        page = data.split(":", 1)[1]
        pages = {
            "group": "👥 GROUP MANAGEMENT\nUse /settings and /help inside your group.",
            "channel": "📢 Channel features are limited; the current bot focuses on groups.",
            "support": "🆘 Contact the bot owner for support.",
            "info": "ℹ️ Asemooh Service Bot provides group moderation, role management and join-request approval.",
            "languages": "🌍 English is currently available.",
            "privacy": "🔒 The bot stores group settings, role assignments, warning counts and observed activity in its local database. Avoid sending sensitive data.",
        }
        info = await context.bot.get_me()
        await q.edit_message_text(pages.get(page, "Asemooh Service Bot"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main menu", callback_data="home")]]))
    elif data == "home":
        info = await context.bot.get_me()
        await q.edit_message_text("👋 Welcome to Asemooh Service Bot.", reply_markup=main_menu(info.username))


RULES = "📜 GROUP RULES\n\n1. Respect all members.\n2. No spam or flooding.\n3. No unwanted advertisements.\n4. No abusive language.\n5. Follow administrator instructions."


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, RULES)


# ---------- join, welcome, activity, automatic filters ----------

async def approve_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return
    try:
        await context.bot.approve_chat_join_request(request.chat.id, request.from_user.id)
        log.info("Approved join request from %s in %s", request.from_user.id, request.chat.id)
    except TelegramError:
        log.exception("Could not approve join request")


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    values = get_settings(chat_id)
    if not values["welcome"]:
        return
    with db() as con:
        row = con.execute("SELECT message FROM custom_welcome WHERE chat_id=?", (chat_id,)).fetchone()
    template = row["message"] if row else "👋 Welcome {name}!\n\nPlease respect our group rules. Type /rules to read them."
    for member in update.effective_message.new_chat_members:
        if member.is_bot:
            continue
        with db() as con:
            con.execute("INSERT INTO activity(chat_id,user_id,username,first_name,last_seen,messages) VALUES(?,?,?,?,?,0) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen",
                        (chat_id, member.id, member.username, member.first_name, int(time.time())))
        safe_name = html.escape(member.full_name)
        body = template.replace("{name}", safe_name).replace("{username}", html.escape(member.username or ""))
        try:
            await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)
        except TelegramError:
            await update.effective_message.reply_text(f"👋 Welcome {member.full_name}!\nPlease read /rules.")


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or chat.type == ChatType.PRIVATE or user.is_bot:
        return
    with db() as con:
        con.execute("""INSERT INTO activity(chat_id,user_id,username,first_name,last_seen,messages)
            VALUES(?,?,?,?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET
            username=excluded.username, first_name=excluded.first_name,
            last_seen=excluded.last_seen, messages=activity.messages+1""",
            (chat.id, user.id, user.username, user.first_name, int(time.time())))


async def automatic_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user or not chat or chat.type == ChatType.PRIVATE or user.is_bot:
        return
    # Tracking is performed by a separate handler in the same group update.
    try:
        if await is_telegram_admin(chat.id, user.id, context):
            return
    except TelegramError:
        pass
    roles = get_all_roles(chat.id, user.id)
    if "free" in roles:
        return
    values = get_settings(chat.id)
    text = (message.text or message.caption or "").lower()

    if values["global_silence"]:
        try:
            await message.delete()
        except TelegramError:
            log.exception("Global silence could not delete message")
        return

    if values["media_block"] and (message.photo or message.video or message.document or message.animation or message.audio or message.voice or message.video_note):
        try:
            await message.delete()
        except TelegramError:
            log.exception("Media block could not delete message")
        return

    if values["links"] and re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", text):
        try:
            await message.delete()
        except TelegramError:
            log.exception("Link protection failed")
        return

    if values["badwords"] and BAD_WORDS and any(re.search(rf"\b{re.escape(word)}\b", text) for word in BAD_WORDS):
        try:
            await message.delete()
        except TelegramError:
            log.exception("Bad word filter failed")
        return

    if values["antispam"]:
        key = (chat.id, user.id)
        now = time.time()
        flood[key].append(now)
        while flood[key] and now - flood[key][0] > FLOOD_WINDOW_SECONDS:
            flood[key].popleft()
        if len(flood[key]) >= FLOOD_LIMIT:
            try:
                await message.delete()
                await context.bot.send_message(chat.id, f"⚠️ {user.first_name}, please stop flooding the group.")
            except TelegramError:
                log.exception("Anti-flood action failed")
            flood[key].clear()

    # Custom trigger/response commands are exact case-insensitive text matches.
    if message.text:
        with db() as con:
            row = con.execute("SELECT response FROM custom_replies WHERE chat_id=? AND trigger=?", (chat.id, message.text.strip().lower())).fetchone()
        if row:
            await message.reply_text(row["response"])


# ---------- moderation commands ----------

async def moderation_target(update: Update, context, action: str):
    if not await ensure_action(update, context, action):
        return None
    user = replied_user(update)
    if not user:
        await reply(update, "Reply to the member's message first, then use this command.")
        return None
    return user


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "moderate")
    if not user: return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
        await reply(update, f"🔨 {user.first_name} has been banned.")
    except TelegramError as e: await reply(update, f"Could not ban member: {e}")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "moderate")
    if not user: return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, user.id, only_if_banned=True)
        await reply(update, f"✅ {user.first_name} has been unbanned.")
    except TelegramError as e: await reply(update, f"Could not unban member: {e}")


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "moderate")
    if not user: return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
        await context.bot.unban_chat_member(update.effective_chat.id, user.id, only_if_banned=True)
        await reply(update, f"👢 {user.first_name} has been removed from the group.")
    except TelegramError as e: await reply(update, f"Could not kick member: {e}")


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "mute")
    if not user: return
    minutes = 0
    if context.args and context.args[0].isdigit(): minutes = max(0, min(int(context.args[0]), 525600))
    until = int(time.time()) + minutes * 60 if minutes else None
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, user.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=until)
        label = f" for {minutes} minutes" if minutes else ""
        await reply(update, f"🔇 {user.first_name} has been muted{label}.")
    except TelegramError as e: await reply(update, f"Could not mute member: {e}")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "mute")
    if not user: return
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, user.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True))
        await reply(update, f"🔊 {user.first_name} has been unmuted.")
    except TelegramError as e: await reply(update, f"Could not unmute member: {e}")


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "moderate")
    if not user: return
    chat_id = update.effective_chat.id
    with db() as con:
        con.execute("INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (chat_id, user.id))
        count = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()["count"]
    if count >= WARNING_LIMIT:
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            with db() as con: con.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            await reply(update, f"🚫 {user.first_name} received {WARNING_LIMIT} warnings and was banned.")
        except TelegramError as e: await reply(update, f"Warning {count}/{WARNING_LIMIT}; automatic ban failed: {e}")
    else:
        await reply(update, f"⚠️ Warning {count}/{WARNING_LIMIT} for {user.first_name}.")


async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "moderate")
    if not user: return
    chat_id = update.effective_chat.id
    with db() as con:
        con.execute("UPDATE warnings SET count=MAX(0,count-1) WHERE chat_id=? AND user_id=?", (chat_id, user.id))
        row = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
    await reply(update, f"✅ Warning removed. Current warnings: {row['count'] if row else 0}.")


async def warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = replied_user(update)
    if not user:
        await reply(update, "Reply to a member's message to check warnings."); return
    with db() as con: row = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (update.effective_chat.id, user.id)).fetchone()
    await reply(update, f"⚠️ {user.first_name} has {row['count'] if row else 0} warning(s).")


async def delwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await moderation_target(update, context, "delete")
    if not user: return
    if update.effective_message.reply_to_message:
        try: await update.effective_message.reply_to_message.delete()
        except TelegramError: pass
    chat_id = update.effective_chat.id
    with db() as con:
        con.execute("INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (chat_id, user.id))
        count = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()["count"]
    await reply(update, f"🗑 Message deleted. {user.first_name} has {count}/{WARNING_LIMIT} warnings.")


# ---------- delete, pins, log channel ----------

async def delete_message(update: Update, context, log_copy: bool):
    if not await ensure_action(update, context, "delete"): return
    message = update.effective_message.reply_to_message
    if not message:
        await reply(update, "Reply to the message you want to delete."); return
    _, log_chat_id = get_link_ids(update.effective_chat.id)
    if log_copy:
        if not log_chat_id:
            await reply(update, "Set a log chat first using /setlog inside the log group."); return
        try:
            await context.bot.copy_message(log_chat_id, update.effective_chat.id, message.message_id)
        except TelegramError as e:
            await reply(update, f"Could not copy to the log chat: {e}"); return
    try:
        await message.delete()
        await update.effective_message.delete()
    except TelegramError as e:
        await reply(update, f"Could not delete the message: {e}")


async def delete_command(update: Update, context): await delete_message(update, context, False)
async def logdel_command(update: Update, context): await delete_message(update, context, True)


async def geturl(update: Update, context):
    message = update.effective_message.reply_to_message
    if not message: await reply(update, "Reply to a message to get its link."); return
    chat = update.effective_chat
    if chat.username:
        url = f"https://t.me/{chat.username}/{message.message_id}"
    elif str(chat.id).startswith("-100"):
        url = f"https://t.me/c/{str(chat.id)[4:]}/{message.message_id}"
    else:
        await reply(update, "This group does not have a public username or supported private link."); return
    await reply(update, f"🔗 Message link: {url}", disable_web_page_preview=True)


async def pin(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    target = update.effective_message.reply_to_message
    try:
        if target:
            target_id = target.message_id
        elif context.args:
            sent = await context.bot.send_message(update.effective_chat.id, " ".join(context.args))
            target_id = sent.message_id
            with db() as con: con.execute("INSERT INTO bot_messages(chat_id,message_id) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id", (update.effective_chat.id, target_id))
        else:
            await reply(update, "Reply to a message or use /pin your message."); return
        await context.bot.pin_chat_message(update.effective_chat.id, target_id, disable_notification=True)
        await reply(update, "📌 Message pinned.")
    except TelegramError as e: await reply(update, f"Could not pin message: {e}")


async def editpin(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    if not context.args: await reply(update, "Usage: /editpin new message text"); return
    with db() as con: row = con.execute("SELECT message_id FROM bot_messages WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
    if not row: await reply(update, "No bot-sent pin is recorded. Use /pin text first."); return
    try:
        await context.bot.edit_message_text(" ".join(context.args), update.effective_chat.id, row["message_id"])
        await reply(update, "✅ Pinned bot message edited.")
    except TelegramError as e: await reply(update, f"Could not edit pinned message: {e}")


async def delpin(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        await reply(update, "✅ The pinned message was unpinned.")
    except TelegramError as e: await reply(update, f"Could not unpin: {e}")


async def repin(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        if not chat.pinned_message: await reply(update, "There is no pinned message."); return
        mid = chat.pinned_message.message_id
        await context.bot.unpin_chat_message(update.effective_chat.id, mid)
        await context.bot.pin_chat_message(update.effective_chat.id, mid, disable_notification=False)
        await reply(update, "📌 Pinned message refreshed.")
    except TelegramError as e: await reply(update, f"Could not repin: {e}")


async def pinned(update: Update, context):
    chat = await context.bot.get_chat(update.effective_chat.id)
    if not chat.pinned_message: await reply(update, "There is no pinned message."); return
    await reply(update, f"📌 Current pinned message ID: {chat.pinned_message.message_id}")


# ---------- staff roles ----------

async def role_command(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, remove: bool = False):
    if not await can_manage_roles(update, context): return
    user = replied_user(update)
    if not user:
        await reply(update, "Reply to the member's message first."); return
    chat_id = update.effective_chat.id
    actor_id = update.effective_user.id
    if role == "cofounder":
        if not await is_telegram_admin(chat_id, user.id, context):
            await reply(update, "A Co-Founder must already be a Telegram group administrator. Promote them in Telegram first."); return
        if await is_creator(chat_id, user.id, context):
            await reply(update, "The Founder cannot be assigned or removed as Co-Founder."); return
    if remove:
        remove_role(chat_id, user.id, role)
        await reply(update, f"✅ {ROLE_LABELS[role]} role removed from {user.first_name}.")
    else:
        assign_role(chat_id, user.id, role, actor_id)
        await reply(update, f"✅ {ROLE_LABELS[role]} role assigned to {user.first_name}.")


def make_role_handler(role: str, remove: bool = False):
    async def handler(update, context):
        await role_command(update, context, role, remove)
    return handler


async def staff(update: Update, context):
    chat_id = update.effective_chat.id
    lines = ["👥 GROUP STAFF"]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        await reply(update, f"Could not load staff: {e}"); return
    for member in admins:
        title = "👑 Founder" if member.status == "creator" else "⚜ Telegram Admin"
        role = get_role(chat_id, member.user.id)
        if role == "cofounder": title = ROLE_LABELS["cofounder"]
        name = html.escape(member.user.full_name)
        lines.append(f"• {title}: {name}")
    with db() as con:
        rows = con.execute("SELECT DISTINCT user_id,role FROM roles WHERE chat_id=? ORDER BY role", (chat_id,)).fetchall()
    for row in rows:
        try:
            user = await context.bot.get_chat(row["user_id"])
            role_name = ROLE_LABELS.get(row["role"], row["role"])
            lines.append(f"• {role_name}: {html.escape(user.full_name)}")
        except TelegramError:
            lines.append(f"• {ROLE_LABELS.get(row['role'], row['role'])}: user {row['user_id']}")
    await reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


async def reload_admins(update: Update, context):
    if not group_only(update) or not await is_telegram_admin(update.effective_chat.id, update.effective_user.id, context):
        await reply(update, "⛔ Telegram administrators only."); return
    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
    await reply(update, f"🔄 Telegram administrator list refreshed. Found {len(admins)} administrator(s). Telegram permissions remain authoritative.")


# ---------- member information ----------

async def info(update: Update, context):
    user = replied_user(update)
    if not user: await reply(update, "Reply to a member's message to view their information."); return
    role_list = get_all_roles(update.effective_chat.id, user.id)
    try: member = await context.bot.get_chat_member(update.effective_chat.id, user.id); status = member.status
    except TelegramError: status = "unknown"
    text = (f"👤 MEMBER INFO\nName: {user.full_name}\nID: {user.id}\n"
            f"Username: @{user.username}" if user.username else f"👤 MEMBER INFO\nName: {user.full_name}\nID: {user.id}\nUsername: none")
    text += f"\nTelegram status: {status}\nBot roles: {', '.join(role_list) if role_list else 'None'}"
    await reply(update, text)


async def infopvt(update: Update, context):
    user = replied_user(update)
    if not user: await reply(update, "Reply to a member's message first."); return
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        txt = f"👤 Member: {user.full_name}\nID: {user.id}\nStatus: {member.status}\nRoles: {', '.join(get_all_roles(update.effective_chat.id,user.id)) or 'None'}"
        await context.bot.send_message(update.effective_user.id, txt)
        await reply(update, "✅ Member information sent to your private chat.")
    except TelegramError:
        await reply(update, "I could not message you privately. Open the bot and press /start first.")


async def me_command(update: Update, context):
    user = update.effective_user
    chat_id = update.effective_chat.id
    if update.effective_chat.type == ChatType.PRIVATE:
        await reply(update, f"👤 Your Telegram ID: {user.id}\nUsername: @{user.username or 'none'}")
        return
    try: member = await context.bot.get_chat_member(chat_id, user.id); status = member.status
    except TelegramError: status = "unknown"
    await reply(update, f"👤 Your group info\nName: {user.full_name}\nID: {user.id}\nStatus: {status}\nBot roles: {', '.join(get_all_roles(chat_id,user.id)) or 'None'}")


# ---------- staff group, log group, intervention, send, list and stats ----------

async def setlog(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    set_link(update.effective_chat.id, "log_chat_id", update.effective_chat.id)
    await reply(update, "✅ This group is now the log destination for the group where you run /setlog. To set a separate log group, use /setlog in that group and then /setlogfrom in the main group with its chat ID.")


async def setlogfrom(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await reply(update, "Usage: /setlogfrom CHAT_ID (run /setlog in the destination group first)."); return
    set_link(update.effective_chat.id, "log_chat_id", int(context.args[0]))
    await reply(update, "✅ Log destination saved. Make sure the bot can post in that group.")


async def staffgroup(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await reply(update, "Usage: /staffgroup STAFF_GROUP_CHAT_ID. The bot must be an administrator in both groups."); return
    staff_id = int(context.args[0])
    try:
        await context.bot.get_chat(staff_id)
        staff_bot_member = await context.bot.get_chat_member(staff_id, context.bot.id)
        if staff_bot_member.status not in ("administrator", "creator"):
            await reply(update, "I must be an administrator in the staff group before it can be connected.")
            return
    except TelegramError:
        await reply(update, "I cannot access that group. Add me there as an administrator and check the chat ID."); return
    set_link(update.effective_chat.id, "staff_chat_id", staff_id)
    await reply(update, "✅ Staff group connected. Reports and /intervention requests can now be sent there.")


async def unstaffgroup(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    set_link(update.effective_chat.id, "staff_chat_id", None)
    await reply(update, "✅ Staff group disconnected.")


async def intervention(update: Update, context):
    staff_id, _ = get_link_ids(update.effective_chat.id)
    if not staff_id: await reply(update, "No staff group is connected. An admin can use /staffgroup CHAT_ID."); return
    msg = update.effective_message.reply_to_message
    detail = " ".join(context.args) if context.args else "Moderator assistance requested."
    origin_link = f"\nChat: {update.effective_chat.title} (ID {update.effective_chat.id})"
    try:
        await context.bot.send_message(staff_id, f"🆘 INTERVENTION REQUEST\n{detail}{origin_link}\nRequested by: {update.effective_user.full_name} (ID {update.effective_user.id})")
        if msg:
            try: await context.bot.copy_message(staff_id, update.effective_chat.id, msg.message_id)
            except TelegramError: pass
        await reply(update, "✅ Your request has been sent to the staff group.")
    except TelegramError as e: await reply(update, f"Could not contact the staff group: {e}")


async def send_command(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    if not context.args: await reply(update, "Usage: /send your message"); return
    await context.bot.send_message(update.effective_chat.id, " ".join(context.args))


async def list_command(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() == "roles":
        with db() as con: rows = con.execute("SELECT user_id,role FROM roles WHERE chat_id=? ORDER BY role,user_id", (chat_id,)).fetchall()
        output = ["👥 SPECIAL ROLES"] + [f"{ROLE_LABELS.get(r['role'],r['role'])}: {r['user_id']}" for r in rows]
        text = "\n".join(output) if len(output) > 1 else "No special roles assigned yet."
    else:
        with db() as con: rows = con.execute("SELECT user_id,first_name,username,messages FROM activity WHERE chat_id=? ORDER BY messages DESC LIMIT 100", (chat_id,)).fetchall()
        output = ["📋 OBSERVED MEMBERS (tracking starts after bot deployment)"]
        output += [f"{r['first_name'] or 'Member'} (@{r['username'] or 'none'}) — {r['messages']} messages — ID {r['user_id']}" for r in rows]
        text = "\n".join(output) if len(output) > 1 else "No member activity has been recorded yet."
    try:
        await context.bot.send_message(update.effective_user.id, text[:4000])
        await reply(update, "✅ The list has been sent to your private chat. Open the bot and press /start if it does not arrive.")
    except TelegramError:
        await reply(update, "I cannot message you privately. Open the bot and press /start first.")


async def inactives(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    days = 7
    if context.args and context.args[0].isdigit(): days = max(1, min(int(context.args[0]), 3650))
    cutoff = int(time.time()) - days * 86400
    with db() as con: rows = con.execute("SELECT user_id,first_name,username,last_seen FROM activity WHERE chat_id=? AND last_seen<? ORDER BY last_seen ASC LIMIT 80", (update.effective_chat.id, cutoff)).fetchall()
    lines = [f"😴 OBSERVED MEMBERS INACTIVE FOR {days}+ DAYS", "Note: this only covers activity observed since tracking began."]
    lines += [f"• {r['first_name'] or 'Member'} (@{r['username'] or 'none'}) — ID {r['user_id']}" for r in rows]
    if not rows: lines.append("No matching tracked members.")
    try:
        await context.bot.send_message(update.effective_user.id, "\n".join(lines)[:4000])
        await reply(update, "✅ Inactive-member list sent privately.")
    except TelegramError:
        await reply(update, "Open the bot and press /start first so I can send you the list.")


async def graphic(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    with db() as con:
        rows = con.execute("SELECT date(last_seen,'unixepoch') AS day,COUNT(*) AS n FROM activity WHERE chat_id=? GROUP BY day ORDER BY day DESC LIMIT 7", (update.effective_chat.id,)).fetchall()
    if not rows: await reply(update, "Not enough tracked activity yet."); return
    maxn = max(r["n"] for r in rows) or 1
    lines = ["📊 OBSERVED ACTIVE MEMBERS — LAST RECORDED DAYS"]
    for r in reversed(rows): lines.append(f"{r['day']} {'▰' * max(1, int(r['n'] * 20 / maxn))} {r['n']}")
    await reply(update, "\n".join(lines))


async def trend(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    with db() as con:
        total = con.execute("SELECT COUNT(*) n FROM activity WHERE chat_id=?", (update.effective_chat.id,)).fetchone()["n"]
        messages = con.execute("SELECT COALESCE(SUM(messages),0) n FROM activity WHERE chat_id=?", (update.effective_chat.id,)).fetchone()["n"]
        active = con.execute("SELECT COUNT(*) n FROM activity WHERE chat_id=? AND last_seen>=?", (update.effective_chat.id, int(time.time())-7*86400)).fetchone()["n"]
    await reply(update, f"📈 GROUP ACTIVITY SNAPSHOT\nObserved members: {total}\nMessages tracked: {messages}\nObserved active in last 7 days: {active}\n\nTracking begins when this version is deployed; historic activity is unavailable.")


# ---------- custom welcome/replies ----------

async def setwelcome(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await reply(update, "Usage: /setwelcome Your welcome text. Use {name} for the new member's name."); return
    with db() as con: con.execute("INSERT INTO custom_welcome(chat_id,message) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET message=excluded.message", (update.effective_chat.id, text))
    await reply(update, "✅ Custom welcome message saved. Use {name} to insert the new member's name.")


async def resetwelcome(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    with db() as con: con.execute("DELETE FROM custom_welcome WHERE chat_id=?", (update.effective_chat.id,))
    await reply(update, "✅ Default welcome message restored.")


async def setreply(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    raw = update.effective_message.text.partition(" ")[2]
    if "|" not in raw:
        await reply(update, "Usage: /setreply trigger | automatic reply"); return
    trigger, response = [p.strip() for p in raw.split("|", 1)]
    if not trigger or not response: await reply(update, "Both trigger and reply are required."); return
    with db() as con: con.execute("INSERT INTO custom_replies(chat_id,trigger,response) VALUES(?,?,?) ON CONFLICT(chat_id,trigger) DO UPDATE SET response=excluded.response", (update.effective_chat.id, trigger.lower(), response))
    await reply(update, f"✅ Automatic reply saved for: {trigger}")


async def delreply(update: Update, context):
    if not await ensure_action(update, context, "settings"): return
    if not context.args: await reply(update, "Usage: /delreply trigger"); return
    with db() as con: cur = con.execute("DELETE FROM custom_replies WHERE chat_id=? AND trigger=?", (update.effective_chat.id, " ".join(context.args).lower()))
    await reply(update, "✅ Automatic reply removed." if cur.rowcount else "No matching trigger was found.")


async def observe_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One catch-all message handler: track activity, greet new members, then moderate."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type == ChatType.PRIVATE:
        return
    if message.new_chat_members:
        await welcome(update, context)
        return
    # Track messages even when they contain media rather than text.
    await track_activity(update, context)
    # Commands are handled by CommandHandler and should not be filtered as spam.
    if message.text and message.text.startswith("/"):
        return
    await automatic_moderation(update, context)


async def admin_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep /admin dashboard, while supporting /admin as an admin-role label on reply."""
    if update.effective_message and update.effective_message.reply_to_message:
        if not await can_manage_roles(update, context):
            return
        target = replied_user(update)
        if not target:
            await reply(update, "Reply to a Telegram administrator's message.")
            return
        try:
            if not await is_telegram_admin(update.effective_chat.id, target.id, context):
                await reply(update, "That member must first be promoted as a Telegram administrator.")
                return
        except TelegramError:
            await reply(update, "I could not verify that member's Telegram admin status.")
            return
        if await is_creator(update.effective_chat.id, target.id, context):
            await reply(update, "The Founder is already the highest role.")
            return
        assign_role(update.effective_chat.id, target.id, "admin", update.effective_user.id)
        await reply(update, f"✅ Admin label recorded for {target.first_name}. Telegram admin permissions remain controlled by Telegram.")
        return
    await admin_panel(update, context)


async def unadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_manage_roles(update, context):
        return
    target = replied_user(update)
    if not target:
        await reply(update, "Reply to the administrator's message first.")
        return
    if await is_creator(update.effective_chat.id, target.id, context):
        await reply(update, "The Founder cannot be removed.")
        return
    remove_role(update.effective_chat.id, target.id, "admin")
    await reply(update, f"✅ Admin label removed from {target.first_name}. To demote a Telegram admin, use the Telegram group admin settings.")


# ---------- command registration ----------

async def post_init(app: Application):
    commands = [
        ("start", "Open the bot menu"), ("help", "Show all commands"), ("rules", "Show group rules"),
        ("admin", "Open dashboard or label a replied Telegram admin"), ("unadmin", "Remove admin role label"),
        ("settings", "Open group settings"), ("admin", "Open admin dashboard"), ("staff", "Show group staff"),
        ("ban", "Ban a replied-to member"), ("unban", "Unban a replied-to member"), ("kick", "Remove a replied-to member"),
        ("mute", "Mute a replied-to member"), ("unmute", "Unmute a replied-to member"),
        ("warn", "Warn a replied-to member"), ("unwarn", "Remove a warning"), ("warns", "Check warnings"),
        ("delwarn", "Delete and warn"), ("del", "Delete a replied-to message"), ("logdel", "Log and delete a message"),
        ("info", "Show member details"), ("infopvt", "Send member details privately"), ("me", "Show your information"),
        ("geturl", "Get a replied message link"), ("pin", "Pin a message"), ("editpin", "Edit bot-sent pin"),
        ("delpin", "Unpin current message"), ("repin", "Repin current message"), ("pinned", "Show current pin"),
        ("list", "List observed members or roles"), ("inactives", "List observed inactive members"),
        ("graphic", "Show activity chart"), ("trend", "Show group activity"), ("intervention", "Request moderator help"),
        ("send", "Send a message through the bot"), ("setwelcome", "Set a custom welcome"), ("resetwelcome", "Restore welcome"),
        ("setreply", "Create an automatic reply"), ("delreply", "Delete an automatic reply"),
        ("staffgroup", "Connect a staff group"), ("unstaffgroup", "Disconnect staff group"),
        ("setlog", "Set this group as log chat"), ("setlogfrom", "Set a separate log destination"), ("reload", "Refresh admin list"),
    ]
    for role, (assign_cmd, remove_cmd) in ROLE_COMMANDS.items():
        commands.extend([(assign_cmd, f"Assign {role} role"), (remove_cmd, f"Remove {role} role")])
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in commands])
    except TelegramError:
        log.exception("Could not publish bot command list")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables; do not paste it into bot.py.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    handlers = [
        CommandHandler("start", start), CommandHandler("help", help_command),
        CommandHandler("admin", admin_dispatch), CommandHandler("unadmin", unadmin_command),
        CommandHandler("settings", admin_panel), CommandHandler("rules", rules_command),
        CommandHandler("ban", ban), CommandHandler("unban", unban), CommandHandler("kick", kick),
        CommandHandler("mute", mute), CommandHandler("unmute", unmute),
        CommandHandler("warn", warn), CommandHandler("unwarn", unwarn), CommandHandler("warns", warns),
        CommandHandler("delwarn", delwarn), CommandHandler("del", delete_command), CommandHandler("logdel", logdel_command),
        CommandHandler("geturl", geturl), CommandHandler("pin", pin), CommandHandler("editpin", editpin),
        CommandHandler("delpin", delpin), CommandHandler("repin", repin), CommandHandler("pinned", pinned),
        CommandHandler("staff", staff), CommandHandler("info", info), CommandHandler("infopvt", infopvt), CommandHandler("me", me_command),
        CommandHandler("reload", reload_admins), CommandHandler("setwelcome", setwelcome), CommandHandler("resetwelcome", resetwelcome),
        CommandHandler("setreply", setreply), CommandHandler("delreply", delreply),
        CommandHandler("staffgroup", staffgroup), CommandHandler("unstaffgroup", unstaffgroup),
        CommandHandler("setlog", setlog), CommandHandler("setlogfrom", setlogfrom),
        CommandHandler("intervention", intervention), CommandHandler("send", send_command),
        CommandHandler("list", list_command), CommandHandler("inactives", inactives),
        CommandHandler("graphic", graphic), CommandHandler("trend", trend),
        ChatJoinRequestHandler(approve_join_request),
        CallbackQueryHandler(callbacks),
    ]
    for role, (assign_cmd, remove_cmd) in ROLE_COMMANDS.items():
        handlers.append(CommandHandler(assign_cmd, make_role_handler(role, False)))
        handlers.append(CommandHandler(remove_cmd, make_role_handler(role, True)))
    # Keep this catch-all last so it does not swallow role commands.
    handlers.append(MessageHandler(filters.ALL, observe_message))
    for handler in handlers:
        app.add_handler(handler)

    log.info("Asemooh Service Bot starting with SQLite database %s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def track_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for user in update.effective_message.new_chat_members:
        if user.is_bot:
            continue
        with db() as con:
            con.execute("INSERT INTO activity(chat_id,user_id,username,first_name,last_seen,messages) VALUES(?,?,?,?,?,0) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen",
                        (chat_id, user.id, user.username, user.first_name, int(time.time())))


if __name__ == "__main__":
    main()

import os
import re
import logging
import time
from collections import defaultdict, deque

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters
)

TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

DEFAULTS = {
    "welcome": True,
    "links": True,
    "antispam": True,
    "badwords": True,
    "warnings": True,
}

settings = defaultdict(lambda: DEFAULTS.copy())
warnings = defaultdict(int)
flood = defaultdict(deque)

BAD_WORDS = ["badword1", "badword2"]

RULES = (
    "📜 GROUP RULES\n\n"
    "1. Respect all members.\n"
    "2. No spam or flooding.\n"
    "3. No unwanted advertisements.\n"
    "4. No abusive language.\n"
    "5. Follow administrator instructions."
)


# MAIN MENU

def main_menu(bot_username):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Add me to a group",
                url=f"https://t.me/{bot_username}?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton("👥 Group", callback_data="group"),
            InlineKeyboardButton("📢 Channel", callback_data="channel")
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="support"),
            InlineKeyboardButton("ℹ️ Information", callback_data="info")
        ],
        [
            InlineKeyboardButton("🌍 Languages", callback_data="languages"),
            InlineKeyboardButton("🔒 Privacy Policy", callback_data="privacy")
        ]
    ])


# ADMIN DASHBOARD

def admin_keyboard(chat_id):
    s = settings[chat_id]

    def switch(name, label):
        return InlineKeyboardButton(
            f"{label}: {'ON 🟢' if s[name] else 'OFF 🔴'}",
            callback_data=f"toggle:{name}"
        )

    return InlineKeyboardMarkup([
        [switch("welcome", "👋 Welcome")],
        [switch("links", "🔗 Link Protection")],
        [switch("antispam", "🛡️ Anti-Flood")],
        [switch("badwords", "🚫 Bad Words")],
        [switch("warnings", "⚠️ Auto-Warnings")],
        [
            InlineKeyboardButton(
                "📜 View Rules",
                callback_data="rules"
            ),
            InlineKeyboardButton(
                "🏠 Main Menu",
                callback_data="home"
            )
        ]
    ])


async def is_admin(update, context):
    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    return member.status in ("administrator", "creator")


# START COMMAND

async def start(update, context):
    bot_info = await context.bot.get_me()

    await update.message.reply_text(
        "👋 Hello!\n\n"
        "🤖 Welcome to Asemooh Service Bot!\n\n"
        "I am your assistant for managing Telegram groups "
        "easily and safely.\n\n"
        "👉 Add me to your group and promote me as an "
        "administrator to activate my moderation features.\n\n"
        "❓ WHICH ARE THE COMMANDS?\n"
        "Press /help to see all available commands.",
        reply_markup=main_menu(bot_info.username)
    )


# HELP COMMAND

async def help_command(update, context):
    await update.message.reply_text(
        "🤖 ASEMOOH SERVICE BOT\n\n"
        "📌 GENERAL COMMANDS\n"
        "/start - Open main menu\n"
        "/help - Show available commands\n"
        "/rules - View group rules\n\n"
        "⚙️ ADMIN COMMANDS\n"
        "/admin - Open administrator dashboard\n"
        "/ban - Ban a replied-to member\n"
        "/unban - Unban a replied-to member\n"
        "/mute - Mute a replied-to member\n"
        "/unmute - Unmute a replied-to member\n"
        "/warn - Give a warning to a replied-to member\n\n"
        "📢 IMPORTANT\n"
        "Most moderation commands must be used inside "
        "a group by an administrator.\n\n"
        "Reply to a member's message before using "
        "ban, unban, mute, unmute or warn."
    )


# ADMIN PANEL

async def admin_panel(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Open /admin inside your group."
        )
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Administrators only."
        )
        return

    await update.message.reply_text(
        "⚙️ ASEMOOH GROUP ADMIN PANEL\n\n"
        "Control your group using the buttons below.",
        reply_markup=admin_keyboard(update.effective_chat.id)
    )


# BUTTON CALLBACKS

async def callbacks(update, context):
    query = update.callback_query
    data = query.data

    chat_id = query.message.chat.id

    if data.startswith("toggle:"):
        member = await context.bot.get_chat_member(
            chat_id,
            query.from_user.id
        )

        if member.status not in ("administrator", "creator"):
            await query.answer(
                "Administrators only!",
                show_alert=True
            )
            return

        name = data.split(":", 1)[1]

        if name not in DEFAULTS:
            await query.answer()
            return

        settings[chat_id][name] = not settings[chat_id][name]

        await query.answer("Setting updated.")
        await query.edit_message_reply_markup(
            reply_markup=admin_keyboard(chat_id)
        )
        return

    await query.answer()

    if data == "rules":
        await query.edit_message_text(
            RULES,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Admin Panel",
                    callback_data="admin"
                )
            ]])
        )
        return

    if data == "admin":
        member = await context.bot.get_chat_member(
            chat_id,
            query.from_user.id
        )

        if member.status not in ("administrator", "creator"):
            await query.answer(
                "Administrators only!",
                show_alert=True
            )
            return

        await query.edit_message_text(
            "⚙️ ASEMOOH ADMIN PANEL",
            reply_markup=admin_keyboard(chat_id)
        )
        return

    if data == "home":
        bot_info = await context.bot.get_me()

        await query.edit_message_text(
            "👋 Hello!\n\n"
            "🤖 Welcome to Asemooh Service Bot!\n\n"
            "Your assistant for safer Telegram groups.",
            reply_markup=main_menu(bot_info.username)
        )
        return

    pages = {
        "group": (
            "👥 GROUP MANAGEMENT\n\n"
            "Asemooh Service Bot helps protect and manage "
            "your Telegram group.\n\n"
            "Add the bot to your group and promote it "
            "as an administrator.\n\n"
            "Use /admin inside your group to open "
            "the moderation dashboard."
        ),

        "channel": (
            "📢 CHANNEL INFORMATION\n\n"
            "Asemooh Service Bot can be added to a Telegram "
            "channel as an administrator.\n\n"
            "Channel management features can be configured "
            "separately."
        ),

        "support": (
            "🆘 SUPPORT\n\n"
            "Thank you for using Asemooh Service Bot!\n\n"
            "For assistance, contact the bot owner "
            "through the administrator of this bot."
        ),

        "info": (
            "ℹ️ ABOUT ASEMOOH SERVICE BOT\n\n"
            "Asemooh Service Bot is designed to help "
            "Telegram group administrators manage "
            "their communities.\n\n"
            "Features include:\n"
            "✅ Welcome messages\n"
            "✅ Link protection\n"
            "✅ Anti-flood protection\n"
            "✅ Warning system\n"
            "✅ Member moderation\n"
            "✅ Administrator dashboard\n"
            "✅ Automatic join-request approval"
        ),

        "languages": (
            "🌍 LANGUAGE SETTINGS\n\n"
            "English is currently the available language.\n\n"
            "More languages can be added in future updates."
        ),

        "privacy": (
            "🔒 PRIVACY POLICY\n\n"
            "Asemooh Service Bot processes Telegram messages "
            "to provide its moderation features.\n\n"
            "The bot may process user IDs, group IDs and "
            "message content for moderation purposes.\n\n"
            "Moderation settings, warning counts and "
            "anti-flood history are stored temporarily "
            "in memory while the bot is running.\n\n"
            "This information may reset when the bot restarts.\n\n"
            "Do not send passwords, financial details or "
            "other sensitive personal information to the bot."
        )
    }

    if data in pages:
        await query.edit_message_text(
            pages[data],
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Main Menu",
                    callback_data="home"
                )
            ]])
        )


# RULES COMMAND

async def rules_command(update, context):
    await update.message.reply_text(RULES)


# AUTOMATIC JOIN-REQUEST APPROVAL

async def approve_join_request(update, context):
    request = update.chat_join_request

    if not request:
        return

    chat_id = request.chat.id
    user_id = request.from_user.id

    try:
        await context.bot.approve_chat_join_request(
            chat_id=chat_id,
            user_id=user_id
        )

        logging.info(
            "Approved join request from user %s in group %s",
            user_id,
            chat_id
        )

    except Exception:
        logging.exception(
            "Could not approve join request from user %s in group %s",
            user_id,
            chat_id
        )


# WELCOME MESSAGES

async def welcome(update, context):
    chat_id = update.effective_chat.id

    if not settings[chat_id]["welcome"]:
        return

    for member in update.message.new_chat_members:
        if not member.is_bot:
            await update.message.reply_text(
                f"👋 Welcome {member.mention_html()}!\n\n"
                "Please respect our group rules.\n"
                "Type /rules to read them.",
                parse_mode="HTML"
            )


# AUTOMATIC MODERATION

async def moderate(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not user or not message.text:
        return

    if chat.type == "private":
        return

    member = await context.bot.get_chat_member(
        chat.id,
        user.id
    )

    if member.status in ("administrator", "creator"):
        return

    s = settings[chat.id]
    text = message.text.lower()

    # LINK PROTECTION

    if s["links"] and re.search(
        r"(https?://|www\.|t\.me/)",
        text
    ):
        try:
            await message.delete()
        except Exception:
            logging.exception("Link deletion failed")
        return

    # BAD WORDS PROTECTION

    if s["badwords"] and any(
        word in text for word in BAD_WORDS
    ):
        try:
            await message.delete()
        except Exception:
            logging.exception("Bad word deletion failed")
        return

    # ANTI-FLOOD PROTECTION

    if s["antispam"]:
        key = (chat.id, user.id)
        now = time.time()

        flood[key].append(now)

        while flood[key] and now - flood[key][0] > 8:
            flood[key].popleft()

        if len(flood[key]) >= 6:
            try:
                await message.delete()

                await context.bot.send_message(
                    chat.id,
                    f"⚠️ {user.first_name}, "
                    "please stop flooding the group!"
                )

            except Exception:
                logging.exception("Flood protection failed")

            flood[key].clear()


# ADMIN COMMAND CHECK

async def admin_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "This command works inside groups."
        )
        return False

    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Administrators only."
        )
        return False

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Reply to the member's message to use this command."
        )
        return False

    return True


# BAN COMMAND

async def ban(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user

    await context.bot.ban_chat_member(
        update.effective_chat.id,
        user.id
    )

    await update.message.reply_text(
        f"🔨 {user.first_name} has been banned."
    )


# UNBAN COMMAND

async def unban(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user

    await context.bot.unban_chat_member(
        update.effective_chat.id,
        user.id
    )

    await update.message.reply_text(
        f"✅ {user.first_name} has been unbanned."
    )


# MUTE COMMAND

async def mute(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user

    await context.bot.restrict_chat_member(
        update.effective_chat.id,
        user.id,
        permissions=ChatPermissions(
            can_send_messages=False
        )
    )

    await update.message.reply_text(
        f"🔇 {user.first_name} has been muted."
    )


# UNMUTE COMMAND

async def unmute(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user

    await context.bot.restrict_chat_member(
        update.effective_chat.id,
        user.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )

    await update.message.reply_text(
        f"🔊 {user.first_name} has been unmuted."
    )


# WARN COMMAND

async def warn(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user
    key = (update.effective_chat.id, user.id)

    warnings[key] += 1
    count = warnings[key]

    if count >= 3:
        await context.bot.ban_chat_member(
            update.effective_chat.id,
            user.id
        )

        warnings[key] = 0

        await update.message.reply_text(
            f"🚫 {user.first_name} received "
            "3 warnings and was banned."
        )

    else:
        await update.message.reply_text(
            f"⚠️ Warning {count}/3 for {user.first_name}."
        )


# START BOT

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    app = Application.builder().token(TOKEN).build()

    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("admin", admin_panel),
        CommandHandler("rules", rules_command),
        CommandHandler("ban", ban),
        CommandHandler("unban", unban),
        CommandHandler("mute", mute),
        CommandHandler("unmute", unmute),
        CommandHandler("warn", warn),

        ChatJoinRequestHandler(approve_join_request),

        CallbackQueryHandler(callbacks),

        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome
        ),

        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            moderate
        )
    ]

    for handler in handlers:
        app.add_handler(handler)

    logging.info("Asemooh Service Bot is starting...")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
