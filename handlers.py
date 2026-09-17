import asyncio
import html
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from params import Params
from rss import (
    check_channels_and_notify,
    fetch_feed_url,
    normalize_feed_url,
    parse_feed,
)
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
                "⚠️ <b>Google OAuth credentials are not configured on the bot server.</b>\n\n"
                "Please configure <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> in the bot's configuration."
            )
            await update.effective_message.reply_html(msg)
            return

        try:
            auth_url, flow = youtube.generate_auth_url(client_id, client_secret)
            user_flows[user_id] = flow
            user_sessions[user_id] = "awaiting_auth_code"

            msg = (
                "👋 Welcome to <b>YTSub</b>!\n\n"
                "To connect your YouTube account and read your subscriptions:\n\n"
                f"1. 👉 <a href=\"{auth_url}\"><b>Click here to Authorize with Google</b></a>\n\n"
                "2. Sign in and grant YouTube access.\n"
                "3. Copy the authorization code (or the full redirected URL) from your browser and paste it here."
            )
            await update.effective_message.reply_html(msg, disable_web_page_preview=True)
        except Exception as e:
            logger.error("Failed to generate auth URL for %s: %s", user_id, e)
            await update.effective_message.reply_text(f"❌ Error starting OAuth flow: {e}")

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
                "⚠️ OAuth credentials already exist for your account. Do you want to redo the OAuth process?",
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
            await query.edit_message_text("🔄 Starting re-authentication...")
            await start_oauth_flow(update, context, user_id)
        elif query.data == "reauth_cancel":
            await query.edit_message_text("❌ Re-authentication cancelled.")

    async def perform_sync_subscriptions(update: Update, user_id: int):
        async def send_fn(uid: int, text: str):
            await update.effective_message.reply_text(text)

        try:
            total, new_count = await youtube.sync_user_subscriptions(
                state, params, user_id, send_message_fn=send_fn
            )
            await update.effective_message.reply_text(
                f"✅ Successfully synced subscriptions.\n"
                f"Total channels tracked: {total} ({new_count} newly added)."
            )
        except Exception as e:
            logger.error("Failed to sync subscriptions for %s: %s", user_id, e)
            await update.effective_message.reply_text(f"❌ Error downloading subscriptions: {e}")

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
                await update.effective_message.reply_text("⚠️ Session expired. Please run /start again.")
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

                await update.effective_message.reply_text("✅ Authentication successful. Downloading subscribed channels...")
                await perform_sync_subscriptions(update, user_id)
            except Exception as e:
                logger.error("Failed to exchange code for %s: %s", user_id, e)
                await update.effective_message.reply_text(
                    f"❌ Authentication failed: {e}\n"
                    "Please make sure the code or redirect URL is correct, or run /start to try again."
                )
            return

    async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        if not state.has_user_oauth_credentials(user_id):
            await update.effective_message.reply_text(
                "⚠️ You haven't connected your YouTube account yet. Please use the /start command."
            )
            return

        await update.effective_message.reply_text("🔄 Redownloading subscribed channels...")
        await perform_sync_subscriptions(update, user_id)

    async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        if not params.is_user_admin(user_id):
            await update.effective_message.reply_text("⛔ This command is only available to administrators.")
            return

        state.reload()

        async def send_fn(target_chat_id: int, text: str, reply_markup: Optional[Any] = None, parse_mode: Optional[Any] = None):
            await app.bot.send_message(
                chat_id=target_chat_id,
                text=text,
                disable_web_page_preview=False,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )

        # Check channels updated more than 5 minutes (300 seconds) ago across all users
        start_time = time.time()
        checked = await check_channels_and_notify(
            state=state,
            send_message_fn=send_fn,
            min_seconds_since_check=300.0
        )
        elapsed = time.time() - start_time
        logger.info("Reload check completed in %.2f seconds (%d channels checked).", elapsed, checked)

        await update.effective_message.reply_text(
            f"🔄 State reloaded from disk. Checked {checked} channel(s) updated more than 5 minutes ago."
        )

    async def cmd_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        args = context.args or []
        subcmd = args[0].lower() if args else "list"

        if subcmd == "list":
            custom_feeds = state.get_user_custom_feeds(user_id)
            if not custom_feeds:
                msg = (
                    "ℹ️ You have no custom feeds configured.\n\n"
                    "Use <code>/custom add &lt;url or channel_id&gt;</code> to add one."
                )
                await update.effective_message.reply_html(msg)
                return

            lines = ["📡 <b>Custom RSS Feeds:</b>\n"]
            for idx, (url, info) in enumerate(custom_feeds.items(), 1):
                title = info.get("title") or "Unknown"
                lines.append(f"{idx}. <b>{html.escape(title)}</b>\n   <code>{html.escape(url)}</code>")

            lines.append("\nTo remove a feed, use:\n<code>/custom remove &lt;number or url&gt;</code>")
            await update.effective_message.reply_html("\n".join(lines))

        elif subcmd == "add":
            if len(args) < 2:
                await update.effective_message.reply_html(
                    "ℹ️ Usage: <code>/custom add &lt;url or channel_id&gt;</code>"
                )
                return

            raw_input = args[1].strip()
            url = normalize_feed_url(raw_input)
            if not (url.startswith("http://") or url.startswith("https://")):
                await update.effective_message.reply_text("❌ Invalid URL or channel ID.")
                return

            try:
                async with aiohttp.ClientSession() as session:
                    xml_text = await fetch_feed_url(session, url)
                feed_author, entries = parse_feed(xml_text)
            except Exception as e:
                await update.effective_message.reply_text(
                    f"❌ Failed to fetch or parse feed from URL ({e}). Please check that the URL is reachable:\n{url}"
                )
                return

            feed_title = feed_author or (entries[0].title if entries else "") or raw_input

            is_new = state.add_custom_feed(user_id=user_id, url=url, title=feed_title)
            if is_new:
                await update.effective_message.reply_html(
                    f"✅ Added custom feed: <b>{html.escape(feed_title)}</b>\n<code>{html.escape(url)}</code>"
                )
            else:
                await update.effective_message.reply_html(
                    f"✅ Updated custom feed: <b>{html.escape(feed_title)}</b>\n<code>{html.escape(url)}</code>"
                )

        elif subcmd == "remove":
            if len(args) < 2:
                await update.effective_message.reply_html(
                    "ℹ️ Usage: <code>/custom remove &lt;number or url&gt;</code>"
                )
                return

            target = args[1].strip()
            removed = state.remove_custom_feed(user_id=user_id, identifier=target)
            if removed:
                removed_title = removed.get("title") or target
                await update.effective_message.reply_html(
                    f"✅ Removed custom feed: <b>{html.escape(removed_title)}</b>"
                )
            else:
                await update.effective_message.reply_html(
                    "❌ Feed not found. Use <code>/custom list</code> to see your feeds."
                )

        else:
            msg = (
                "📡 <b>Custom Feed Commands:</b>\n\n"
                "• <code>/custom</code> or <code>/custom list</code> - List your custom feeds\n"
                "• <code>/custom add &lt;url or channel_id&gt;</code> - Add a custom RSS feed\n"
                "• <code>/custom remove &lt;number or url&gt;</code> - Remove a custom RSS feed"
            )
            await update.effective_message.reply_html(msg)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        has_creds = state.has_user_oauth_credentials(user_id)
        user_info = state.get_user(user_id)
        channels = user_info.get("channels", {})
        custom_feeds = user_info.get("custom_feeds", {})
        channel_count = len(channels)
        custom_feed_count = len(custom_feeds)

        pending_count = len(user_info.get("pending_notifications", []))

        msg = (
            f"📊 <b>YTSub Bot Status</b>\n"
            f"• Authenticated: {'Yes' if has_creds else 'No'}\n"
            f"• Tracked channels: {channel_count}\n"
            f"• Custom feeds: {custom_feed_count}\n"
            f"• Check interval: {params.check_interval_sec} seconds"
        )
        if pending_count > 0:
            msg += f"\n• Pending notifications: {pending_count}"

        error_feeds = []
        for ch_id, ch_info in channels.items():
            err_count = ch_info.get("error_count", 0)
            if err_count > 0:
                title = ch_info.get("title") or ch_id
                error_feeds.append((title, err_count, ch_info.get("last_error")))

        for f_url, f_info in custom_feeds.items():
            err_count = f_info.get("error_count", 0)
            if err_count > 0:
                title = f_info.get("title") or f_url
                error_feeds.append((title, err_count, f_info.get("last_error")))

        if error_feeds:
            error_lines = ["\n\n<b>Feeds with errors:</b>"]
            for title, err_count, last_err in error_feeds[:10]:
                err_detail = f": {html.escape(str(last_err))}" if last_err else ""
                error_lines.append(f"• <b>{html.escape(title)}</b> ({err_count} error{'s' if err_count != 1 else ''}{err_detail})")
            if len(error_feeds) > 10:
                error_lines.append(f"...and {len(error_feeds) - 10} more")
            msg += "\n".join(error_lines)

        await update.effective_message.reply_html(msg)

    async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        cleared = state.clear_pending_notifications(user_id)
        if cleared > 0:
            await update.effective_message.reply_html(
                f"🗑️ Cleared {cleared} pending notification{'s' if cleared != 1 else ''}."
            )
        else:
            await update.effective_message.reply_html("ℹ️ No pending notifications in queue.")

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await check_access(update):
            return

        user_id = update.effective_user.id
        is_admin = params.is_user_admin(user_id)

        commands = [
            "/start - Connect or re-authenticate your YouTube account",
            "/update - Redownload list of subscribed channels",
            "/custom - Manage custom RSS feeds (list/add/remove)",
            "/stop - Clear your pending notification queue",
        ]
        if is_admin:
            commands.append("/reload - Reload state from disk and check channels updated >5 min ago")
        commands.extend([
            "/status - Show current tracking status",
            "/help - Show this help message"
        ])

        msg = "ℹ️ <b>YTSub Commands:</b>\n\n" + "\n".join(commands)
        await update.effective_message.reply_html(msg)

    async def callback_playlist_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = update.effective_user
        if not user or not params.is_user_allowed(user.id):
            await query.answer("⛔ Access denied.", show_alert=True)
            return

        user_id = user.id
        data = query.data or ""
        if not (data.startswith("wl:") or data.startswith("ll:") or data.startswith("rwl:") or data.startswith("rll:")):
            await query.answer()
            return

        action, video_id = data.split(":", 1)
        is_remove = action.startswith("r")
        base_action = action[1:] if is_remove else action

        user_info = state.get_user(user_id)
        token = user_info.get("token")
        refresh_token = user_info.get("refresh_token")
        if not (token or refresh_token):
            await query.answer("⚠️ Please connect your YouTube account with /start first.", show_alert=True)
            return

        client_id = params.google_client_id
        client_secret = params.google_client_secret
        if not (client_id and client_secret):
            await query.answer("⚠️ Google credentials not configured on the bot server.", show_alert=True)
            return

        if base_action == "wl":
            playlist_key = "watch_later"
            target_title = "YTSub Watch Later"
            unadded_text = "🕒 Watch Later"
            added_text = "✅ Watch Later"
            add_cb = f"wl:{video_id}"
            remove_cb = f"rwl:{video_id}"
        else:
            playlist_key = "listen_later"
            target_title = "YTSub Listen Later"
            unadded_text = "🎧 Listen Later"
            added_text = "✅ Listen Later"
            add_cb = f"ll:{video_id}"
            remove_cb = f"rll:{video_id}"

        try:
            playlist_id = state.get_user_playlist(user_id, playlist_key)

            if is_remove:
                # Remove from playlist
                if playlist_id:
                    try:
                        _, refreshed_tok = youtube.remove_video_from_playlist(
                            client_id=client_id,
                            client_secret=client_secret,
                            token=token,
                            refresh_token=refresh_token,
                            playlist_id=playlist_id,
                            video_id=video_id
                        )
                        if refreshed_tok:
                            state.set_user_tokens(user_id, token=refreshed_tok, refresh_token=refresh_token)
                    except Exception as e:
                        err_str = str(e)
                        if "playlistNotFound" in err_str or "404" in err_str:
                            state.clear_user_playlist(user_id, playlist_key)
                        else:
                            raise

                await query.answer(f"🗑️ Removed from {target_title}")
                next_text = unadded_text
                next_cb = add_cb
            else:
                # Add to playlist
                if not playlist_id:
                    playlist_id, refreshed_tok = youtube.find_or_create_playlist(
                        client_id=client_id,
                        client_secret=client_secret,
                        token=token,
                        refresh_token=refresh_token,
                        title=target_title
                    )
                    if refreshed_tok:
                        state.set_user_tokens(user_id, token=refreshed_tok, refresh_token=refresh_token)
                        token = refreshed_tok
                    state.set_user_playlist(user_id, playlist_key, playlist_id)

                try:
                    _, refreshed_tok = youtube.add_video_to_playlist(
                        client_id=client_id,
                        client_secret=client_secret,
                        token=token,
                        refresh_token=refresh_token,
                        playlist_id=playlist_id,
                        video_id=video_id
                    )
                    if refreshed_tok:
                        state.set_user_tokens(user_id, token=refreshed_tok, refresh_token=refresh_token)
                except Exception as e:
                    err_str = str(e)
                    if "playlistNotFound" in err_str or "404" in err_str:
                        state.clear_user_playlist(user_id, playlist_key)
                        playlist_id, refreshed_tok = youtube.find_or_create_playlist(
                            client_id=client_id,
                            client_secret=client_secret,
                            token=token,
                            refresh_token=refresh_token,
                            title=target_title
                        )
                        if refreshed_tok:
                            state.set_user_tokens(user_id, token=refreshed_tok, refresh_token=refresh_token)
                            token = refreshed_tok
                        state.set_user_playlist(user_id, playlist_key, playlist_id)

                        _, refreshed_tok = youtube.add_video_to_playlist(
                            client_id=client_id,
                            client_secret=client_secret,
                            token=token,
                            refresh_token=refresh_token,
                            playlist_id=playlist_id,
                            video_id=video_id
                        )
                        if refreshed_tok:
                            state.set_user_tokens(user_id, token=refreshed_tok, refresh_token=refresh_token)
                    else:
                        raise

                await query.answer(f"✅ Added to {target_title}")
                next_text = added_text
                next_cb = remove_cb

            # Update inline button
            if query.message and query.message.reply_markup:
                new_keyboard = []
                for row in query.message.reply_markup.inline_keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data == data:
                            new_row.append(
                                InlineKeyboardButton(
                                    next_text,
                                    callback_data=next_cb
                                )
                            )
                        else:
                            new_row.append(btn)
                    new_keyboard.append(new_row)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup(new_keyboard)
                    )
                except Exception as edit_err:
                    logger.debug("Failed to edit reply markup: %s", edit_err)

        except Exception as e:
            action_name = "removing" if is_remove else "adding"
            logger.error("Error %s video %s to playlist '%s': %s", action_name, video_id, target_title, e)
            await query.answer(f"❌ Failed: {e}", show_alert=True)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("custom", cmd_custom))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_reauth, pattern=r"^reauth_"))
    app.add_handler(CallbackQueryHandler(callback_playlist_action, pattern=r"^(wl|ll|rwl|rll):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


