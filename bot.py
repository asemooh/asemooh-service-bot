
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
