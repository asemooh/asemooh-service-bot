
import os
import re
import logging
import time
from collections import defaultdict, deque

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

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


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Group", callback_data="group"),
            InlineKeyboardButton("📢 Channel", callback_data="channel")
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="support"),
            InlineKeyboardButton("💬 Information", callback_data="info")
        ],
        [InlineKeyboardButton("🌍 Languages", callback_data="languages")]
    ])


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
            InlineKeyboardButton("📜 View Rules", callback_data="rules"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="home")
        ]
    ])


async def is_admin(update, context):
    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    return member.status in ("administrator", "creator")


async def start(update, context):
    await update.message.reply_text(
        "👋 Welcome to Asemooh Service Bot!\n\n"
        "Your assistant for safer Telegram groups.\n\n"
        "Add me to your group and make me an administrator.",
        reply_markup=main_menu()
    )


async def help_command(update, context):
    await update.message.reply_text(
        "🤖 ASEMOOH SERVICE BOT\n\n"
        "/start - Main menu\n"
        "/help - Help\n"
        "/admin - Administrator dashboard\n"
        "/rules - Group rules\n"
        "/ban - Ban replied-to member\n"
        "/unban - Unban replied-to member\n"
        "/mute - Mute replied-to member\n"
        "/unmute - Unmute replied-to member\n"
        "/warn - Warn replied-to member\n"
    )


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


async def callbacks(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.message.chat.id

    if data.startswith("toggle:"):
        member = await context.bot.get_chat_member(
            chat_id, query.from_user.id
        )

        if member.status not in ("administrator", "creator"):
            await query.answer(
                "Administrators only!", show_alert=True
            )
            return

        name = data.split(":", 1)[1]

        if name not in DEFAULTS:
            return

        settings[chat_id][name] = not settings[chat_id][name]

        await query.edit_message_reply_markup(
            reply_markup=admin_keyboard(chat_id)
        )
        return

    if data == "rules":
        await query.edit_message_text(
            RULES,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Admin Panel", callback_data="admin"
                )
            ]])
        )
        return

    if data == "admin":
        member = await context.bot.get_chat_member(
            chat_id, query.from_user.id
        )
        if member.status not in ("administrator", "creator"):
            await query.answer(
                "Administrators only!", show_alert=True
            )
            return

        await query.edit_message_text(
            "⚙️ ASEMOOH ADMIN PANEL",
            reply_markup=admin_keyboard(chat_id)
        )
        return

    pages = {
        "group": "👥 Group management\n\nUse /admin inside your group.",
        "channel": "📢 Channel management\n\nChannel features can be configured separately.",
        "support": "🆘 Contact your bot administrator for support.",
        "info": "💬 Asemooh Service Bot helps manage Telegram groups.",
        "languages": "🌍 English is currently available."
    }

    if data == "home":
        await query.edit_message_text(
            "👋 Welcome to Asemooh Service Bot!",
            reply_markup=main_menu()
        )
    elif data in pages:
        await query.edit_message_text(
            pages[data],
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⬅️ Main Menu", callback_data="home"
                )
            ]])
        )


async def rules_command(update, context):
    await update.message.reply_text(RULES)


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


async def moderate(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not user or not message.text:
        return

    if chat.type == "private":
        return

    member = await context.bot.get_chat_member(chat.id, user.id)

    if member.status in ("administrator", "creator"):
        return

    s = settings[chat.id]
    text = message.text.lower()

    if s["links"] and re.search(
        r"(https?://|www\.|t\.me/)", text
    ):
        try:
            await message.delete()
        except Exception:
            logging.exception("Link deletion failed")
        return

    if s["badwords"] and any(
        word in text for word in BAD_WORDS
    ):
        try:
            await message.delete()
        except Exception:
            logging.exception("Bad word deletion failed")
        return

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
            f"⚠️ {user.first_name}, please stop flooding the group!"
        )
    except Exception:
        logging.exception("Flood protection failed")
    flood[key].clear()


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


async def ban(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(
        update.effective_chat.id, user.id
    )
    await update.message.reply_text(
        f"🔨 {user.first_name} has been banned."
    )


async def unban(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user
    await context.bot.unban_chat_member(
        update.effective_chat.id, user.id
    )
    await update.message.reply_text(
        f"✅ {user.first_name} has been unbanned."
    )


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


async def warn(update, context):
    if not await admin_command(update, context):
        return

    user = update.message.reply_to_message.from_user
    key = (update.effective_chat.id, user.id)
    warnings[key] += 1

    count = warnings[key]

    if count >= 3:
        await context.bot.ban_chat_member(
            update.effective_chat.id, user.id
        )
        warnings[key] = 0
        await update.message.reply_text(
            f"🚫 {user.first_name} received 3 warnings and was banned."
        )
    else:
        await update.message.reply_text(
            f"⚠️ Warning {count}/3 for {user.first_name}."
        )


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
        CallbackQueryHandler(callbacks),
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome
        ),
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, moderate
        )
    ]

    for handler in handlers:
        app.add_handler(handler)

    logging.info("Asemooh Service Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

