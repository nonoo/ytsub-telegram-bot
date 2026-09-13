import json
import logging
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from params import Params
from rss import check_channels_and_notify
from state import StateManager
import youtube

logger = logging.getLogger(__name__)

# In-memory session tracking for onboarding steps
# user_id -> "awaiting_client_creds" | "awaiting_auth_code"
user_sessions: Dict[int, str] = {}
# user_id -> flow object
user_flows: Dict[int, Any] = {}


def setup_handlers(app, params: Params, state: StateManager):
    async def check_access(update: Update) -> bool:
        user = update.effective_user
        if not user or not params.is_user_allowed(user.id):
            if update.effective_message:
                logger.warning("Unauthorized access attempt by user %s", user.id if user else "Unknown")
            return False
        return True

    async def start_oauth_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
        client_id = params.google_client_id
        client_secret = params.google_client_secret

        if not client_id or not client_secret:
            msg = (
                "Welcome to <b>YTSub</b>!\n\n"
                "Google OAuth credentials are not configured on the bot server.\n\n"
                "Please configure <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> in the bot's configuration."
            )
            await update.effective_message.reply_html(msg)
            return

        try:
            auth_url, flow = youtube.generate_auth_url(client_id, client_secret)
            user_flows[user_id] = flow
            user_sessions[user_id] = "awaiting_auth_code"

            msg = (
                "Welcome to <b>YTSub</b>!\n\n"
                "To connect your YouTube account and read your subscriptions:\n\n"
                f"1. 👉 <a href=\"{auth_url}\"><b>Click here to Authorize with Google</b></a>\n\n"
                "2. Sign in and grant YouTube read access.\n"
                "3. Copy the authorization code (or the full redirected URL) from your browser and paste it here."
            )
            await update.effective_message.reply_html(msg, disable_web_page_preview=True)
        except Exception as e:
            logger.error("Failed to generate auth URL for %s: %s", user_id, e)
            await update.effective_message.reply_text(f"Error starting OAuth flow: {e}")

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        if state.has_user_oauth_credentials(user_id):
            keyboard = [
                [
                    InlineKeyboardButton("Yes, re-authenticate", callback_data="reauth_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="reauth_cancel"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.effective_message.reply_text(
                "OAuth credentials already exist for your account. Do you want to redo the OAuth process?",
                reply_markup=reply_markup
            )
            return

        await start_oauth_flow(update, context, user_id)

    async def callback_reauth(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        if not params.is_user_allowed(user_id):
            return

        if query.data == "reauth_confirm":
            await query.edit_message_text("Starting re-authentication...")
            await start_oauth_flow(update, context, user_id)
        elif query.data == "reauth_cancel":
            await query.edit_message_text("Re-authentication cancelled.")

    async def perform_sync_subscriptions(update: Update, user_id: int):
        client_id = params.google_client_id
        client_secret = params.google_client_secret
        user_info = state.get_user(user_id)
        token = user_info.get("token", "")
        refresh_token = user_info.get("refresh_token", "")

        try:
            channels, refreshed_token = youtube.fetch_user_subscriptions(client_id, client_secret, token, refresh_token)
            if refreshed_token:
                state.set_user_tokens(user_id, token=refreshed_token, refresh_token=refresh_token)

            new_count = state.sync_user_channels(user_id, channels)
            total = len(channels)
            await update.effective_message.reply_text(
                f"Successfully synced subscriptions.\n"
                f"Total channels tracked: {total} ({new_count} newly added)."
            )
        except Exception as e:
            logger.error("Failed to sync subscriptions for %s: %s", user_id, e)
            await update.effective_message.reply_text(f"Error downloading subscriptions: {e}")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        session_step = user_sessions.get(user_id)
        text = update.effective_message.text.strip() if update.effective_message.text else ""

        if session_step == "awaiting_auth_code":
            flow = user_flows.get(user_id)
            if not flow:
                user_sessions.pop(user_id, None)
                await update.effective_message.reply_text("Session expired. Please run /start again.")
                return

            try:
                token, refresh_token = youtube.exchange_code_for_tokens(flow, text)
                state.set_user_tokens(
                    user_id=user_id,
                    token=token,
                    refresh_token=refresh_token
                )
                user_sessions.pop(user_id, None)
                user_flows.pop(user_id, None)

                await update.effective_message.reply_text("Authentication successful! Downloading subscribed channels...")
                await perform_sync_subscriptions(update, user_id)
            except Exception as e:
                logger.error("Failed to exchange code for %s: %s", user_id, e)
                await update.effective_message.reply_text(
                    f"Authentication failed: {e}\n"
                    "Please make sure the code or redirect URL is correct, or run /start to try again."
                )
            return

    async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        if not state.has_user_oauth_credentials(user_id):
            await update.effective_message.reply_text(
                "You haven't connected your YouTube account yet. Please use the /start command."
            )
            return

        await update.effective_message.reply_text("Redownloading subscribed channels...")
        await perform_sync_subscriptions(update, user_id)

    async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        if not params.is_user_admin(user_id):
            await update.effective_message.reply_text("This command is only available to administrators.")
            return

        state.reload()

        async def send_fn(target_chat_id: int, text: str):
            await app.bot.send_message(chat_id=target_chat_id, text=text, disable_web_page_preview=False)

        # Check channels updated more than 5 minutes (300 seconds) ago across all users
        checked = await check_channels_and_notify(
            state=state,
            send_message_fn=send_fn,
            min_seconds_since_check=300.0
        )

        await update.effective_message.reply_text(
            f"State reloaded from disk. Checked {checked} channel(s) updated more than 5 minutes ago."
        )

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        has_creds = state.has_user_oauth_credentials(user_id)
        user_info = state.get_user(user_id)
        channel_count = len(user_info.get("channels", {}))

        msg = (
            f"<b>YTSub Bot Status</b>\n"
            f"• Authenticated: {'Yes' if has_creds else 'No'}\n"
            f"• Tracked channels: {channel_count}\n"
            f"• Check interval: {params.check_interval_sec} seconds"
        )
        await update.effective_message.reply_html(msg)

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        is_admin = params.is_user_admin(user_id)

        commands = [
            "/start - Connect or re-authenticate your YouTube account",
            "/update - Redownload list of subscribed channels",
        ]
        if is_admin:
            commands.append("/reload - Reload state from disk and check channels updated >5 min ago")
        commands.extend([
            "/status - Show current tracking status",
            "/help - Show this help message"
        ])

        msg = "<b>YTSub Commands:</b>\n\n" + "\n".join(commands)
        await update.effective_message.reply_html(msg)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_reauth))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
