
import os
import logging
from collections import defaultdict

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

warnings = defaultdict(int)

WELCOME = (
    "👋 Welcome to the group!\n\n"
    "Please read the group rules and respect other members.\n"
    "Enjoy your stay! ❤️"
)

RULES = (
    "📜 GROUP RULES\n\n"
    "1. Respect every member.\n"
    "2. No spam or flooding.\n"
    "3. No unwanted advertising.\n"
    "4. No abusive language.\n"
    "5. Follow the administrators' instructions."
)


def main_menu():
    keyboard = [
        [
            InlineKeyboardButton("👥 Group", callback_data="group"),
            InlineKeyboardButton("📢 Channel", callback_data="channel")
        ],
        [
            InlineKeyboardButton("🆘 Support", callback_data="support"),
            InlineKeyboardButton("💬 Information", callback_data="info")
        ],
        [
            InlineKeyboardButton("🌍 Languages", callback_data="languages")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Asemooh Service Bot!\n\n"
        "Your assistant for safer and better Telegram groups.\n\n"
        "Add me to your group and make me an administrator "
        "to activate group management.",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ASE MOOH SERVICE BOT — COMMANDS\n\n"
        "/start - Main menu\n"
        "/help - Show commands\n"
        "/rules - View group rules\n"
        "/ban - Ban a replied-to member\n"
        "/unban - Unban a replied-to member\n"
        "/mute - Mute a replied-to member\n"
        "/unmute - Unmute a replied-to member\n"
        "/warn - Warn a replied-to member\n"
        "/id - Show chat and user IDs\n\n"
        "Moderation commands require group administrator rights."
    )


async def menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    pages = {
        "group": (
            "👥 GROUP MANAGEMENT\n\n"
            "Add the bot to your group and promote it to admin.\n"
            "Use /help to see moderation commands."
        ),
        "channel": (
            "📢 CHANNEL MANAGEMENT\n\n"
            "Add the bot as a channel administrator "
            "with the permissions it needs."
        ),
        "support": (
            "🆘 SUPPORT\n\n"
            "For assistance, contact the bot owner."
        ),
        "info": (
            "💬 ABOUT ASEMOOH SERVICE BOT\n\n"
            "A Telegram assistant for group moderation, "
            "welcome messages and spam protection."
        ),
        "languages": (
            "🌍 LANGUAGES\n\n"
            "English is currently available."
        )
    }

    await query.edit_message_text(
        pages.get(query.data, "Welcome to Asemooh Bot."),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Main menu", callback_data="home")
        ]])
    )


async def home_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👋 Welcome to Asemooh Service Bot!\n"
        "Choose an option below:",
        reply_markup=main_menu()
    )


async def is_admin(update, context):
    member = await context.bot.get_chat_member(
        update.effective_chat.id,
        update.effective_user.id
    )
    return member.status in ("administrator", "creator")


async def admin_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "This command works inside groups."
        )
        return False

    if not await is_admin(update, context):
        await update.message.reply_text(
            "⛔ Only group administrators can use this command."
        )
        return False

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Reply to the member's message when using this command."
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
        permissions=ChatPermissions(can_send_messages=False)
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
        f"🔊 {user.first_name} can now send messages."
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


async def rules(update, context):
    await update.message.reply_text(RULES)


async def show_id(update, context):
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\n"
        f"Your ID: {update.effective_user.id}"
    )


async def welcome(update, context):
    for member in update.message.new_chat_members:
        if not member.is_bot:
            await update.message.reply_text(
                f"👋 Welcome, {member.mention_html()}!\n\n"
                "Please read the group rules. Type /rules to view them.",
                parse_mode="HTML"
            )


async def anti_spam(update, context):
    message = update.effective_message
    if not message or not message.text:
        return

    if update.effective_chat.type == "private":
        return

    sender = update.effective_user
    if not sender:
        return

    member = await context.bot.get_chat_member(
        update.effective_chat.id, sender.id
    )

    if member.status in ("administrator", "creator"):
        return

    text = message.text.lower()
    blocked = (
        "http://" in text
        or "https://" in text
        or "t.me/" in text
        or "www." in text
    )

    if blocked:
        try:
            await message.delete()
        except Exception:
            logger.exception("Could not delete a spam message")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("warn", warn))

    app.add_handler(CallbackQueryHandler(
        home_callback, pattern="^home$"
    ))
    app.add_handler(CallbackQueryHandler(
        menu_callback,
        pattern="^(group|channel|support|info|languages)$"
    ))

    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, anti_spam
    ))

    logger.info("Asemooh Service Bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
