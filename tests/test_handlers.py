import logging
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers import setup_handlers
from params import Params
from state import StateManager


@pytest.mark.asyncio
async def test_unauthenticated_user_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        update_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "update" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply = AsyncMock()
        mock_update.effective_message.reply_text = mock_reply

        context = MagicMock()

        # Call /update without OAuth credentials
        await update_handler.callback(mock_update, context)
        mock_reply.assert_called_with("You haven't connected your YouTube account yet. Please use the /start command.")


@pytest.mark.asyncio
async def test_reload_admin_restriction():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001, 2002]
        params.admin_user_ids = [2002]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        reload_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "reload" in h.commands)

        # 1. Non-admin user (1001) calls /reload
        mock_update_user = MagicMock()
        mock_update_user.effective_user.id = 1001
        mock_reply_user = AsyncMock()
        mock_update_user.effective_message.reply_text = mock_reply_user

        context = MagicMock()
        await reload_handler.callback(mock_update_user, context)
        mock_reply_user.assert_called_with("This command is only available to administrators.")

        # 2. Admin user (2002) calls /reload
        mock_update_admin = MagicMock()
        mock_update_admin.effective_user.id = 2002
        mock_reply_admin = AsyncMock()
        mock_update_admin.effective_message.reply_text = mock_reply_admin

        with patch("handlers.check_channels_and_notify", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = 3
            await reload_handler.callback(mock_update_admin, context)
            assert "Checked 3 channel(s)" in mock_reply_admin.call_args[0][0]


@pytest.mark.asyncio
async def test_help_command_admin_visibility():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001, 2002]
        params.admin_user_ids = [2002]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        help_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "help" in h.commands)

        # Non-admin user (1001) calls /help
        mock_update_user = MagicMock()
        mock_update_user.effective_user.id = 1001
        mock_reply_html_user = AsyncMock()
        mock_update_user.effective_message.reply_html = mock_reply_html_user

        context = MagicMock()
        await help_handler.callback(mock_update_user, context)
        user_help_text = mock_reply_html_user.call_args[0][0]
        assert "/reload" not in user_help_text
        assert "/start" in user_help_text
        assert "/update" in user_help_text

        # Admin user (2002) calls /help
        mock_update_admin = MagicMock()
        mock_update_admin.effective_user.id = 2002
        mock_reply_html_admin = AsyncMock()
        mock_update_admin.effective_message.reply_html = mock_reply_html_admin

        await help_handler.callback(mock_update_admin, context)
        admin_help_text = mock_reply_html_admin.call_args[0][0]
        assert "/reload" in admin_help_text
        assert "/start" in admin_help_text
        assert "/update" in admin_help_text


@pytest.mark.asyncio
async def test_start_with_server_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.google_client_id = "test-cid"
        params.google_client_secret = "test-secret"

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        start_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "start" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html

        context = MagicMock()

        with patch("youtube.generate_auth_url") as mock_gen:
            mock_gen.return_value = ("https://accounts.google.com/o/oauth2/auth?test", MagicMock())
            await start_handler.callback(mock_update, context)

            mock_gen.assert_called_with("test-cid", "test-secret")
            reply_text = mock_reply_html.call_args[0][0]
            assert "Click here to Authorize with Google" in reply_text
            assert "https://accounts.google.com/o/oauth2/auth?test" in reply_text


@pytest.mark.asyncio
async def test_start_without_server_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        # No google credentials set

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        start_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "start" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html

        context = MagicMock()
        await start_handler.callback(mock_update, context)

        reply_text = mock_reply_html.call_args[0][0]
        assert "Google OAuth credentials are not configured" in reply_text


def test_telegram_get_updates_filter():
    from main import TelegramGetUpdatesFilter
    f = TelegramGetUpdatesFilter()
    rec_drop = logging.LogRecord(
        "httpx", logging.INFO, "pathname", 1,
        'HTTP Request: POST https://api.telegram.org/bot123/getUpdates "HTTP/1.1 200 OK"', (), None
    )
    assert f.filter(rec_drop) is False

    rec_keep = logging.LogRecord(
        "httpx", logging.INFO, "pathname", 1,
        'HTTP Request: POST https://api.telegram.org/bot123/sendMessage "HTTP/1.1 200 OK"', (), None
    )
    assert f.filter(rec_keep) is True


@pytest.mark.asyncio
async def test_callback_playlist_action_watch_later():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.set_user_tokens(1001, "tok-test", "ref-test")

        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.google_client_id = "test-cid"
        params.google_client_secret = "test-csec"

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        from telegram.ext import CallbackQueryHandler
        playlist_handler = next(
            h for h in registered_handlers
            if isinstance(h, CallbackQueryHandler) and getattr(h, "pattern", None) and "wl" in h.pattern.pattern
        )

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_query = AsyncMock()
        mock_query.data = "wl:test_vid_1"
        mock_query.message.reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🕒 Watch Later", callback_data="wl:test_vid_1"),
                InlineKeyboardButton("🎧 Listen Later", callback_data="ll:test_vid_1")
            ]
        ])
        mock_update.callback_query = mock_query
        context = MagicMock()

        with patch("youtube.find_or_create_playlist") as mock_find_create, \
             patch("youtube.add_video_to_playlist") as mock_add_video:
            mock_find_create.return_value = ("PL_wl_id", None)
            mock_add_video.return_value = (True, None)

            await playlist_handler.callback(mock_update, context)

            mock_find_create.assert_called_once_with(
                client_id="test-cid",
                client_secret="test-csec",
                token="tok-test",
                refresh_token="ref-test",
                title="YTSub Watch Later"
            )
            mock_add_video.assert_called_once_with(
                client_id="test-cid",
                client_secret="test-csec",
                token="tok-test",
                refresh_token="ref-test",
                playlist_id="PL_wl_id",
                video_id="test_vid_1"
            )
            assert sm.get_user_playlist(1001, "watch_later") == "PL_wl_id"
            mock_query.answer.assert_called_with("Added to YTSub Watch Later")

            # Verify button text and callback_data updated
            mock_query.edit_message_reply_markup.assert_called_once()
            new_markup = mock_query.edit_message_reply_markup.call_args[1]["reply_markup"]
            assert new_markup.inline_keyboard[0][0].text == "✅ Watch Later"
            assert new_markup.inline_keyboard[0][0].callback_data == "rwl:test_vid_1"


@pytest.mark.asyncio
async def test_callback_playlist_action_toggle_removal():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.set_user_tokens(1001, "tok-test", "ref-test")
        sm.set_user_playlist(1001, "watch_later", "PL_wl_id")

        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.google_client_id = "test-cid"
        params.google_client_secret = "test-csec"

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        from telegram.ext import CallbackQueryHandler
        playlist_handler = next(
            h for h in registered_handlers
            if isinstance(h, CallbackQueryHandler) and getattr(h, "pattern", None) and "rwl" in h.pattern.pattern
        )

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_query = AsyncMock()
        mock_query.data = "rwl:test_vid_1"
        mock_query.message.reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Watch Later", callback_data="rwl:test_vid_1"),
                InlineKeyboardButton("🎧 Listen Later", callback_data="ll:test_vid_1")
            ]
        ])
        mock_update.callback_query = mock_query
        context = MagicMock()

        with patch("youtube.remove_video_from_playlist") as mock_remove_video:
            mock_remove_video.return_value = (True, None)

            await playlist_handler.callback(mock_update, context)

            mock_remove_video.assert_called_once_with(
                client_id="test-cid",
                client_secret="test-csec",
                token="tok-test",
                refresh_token="ref-test",
                playlist_id="PL_wl_id",
                video_id="test_vid_1"
            )
            mock_query.answer.assert_called_with("Removed from YTSub Watch Later")

            # Verify button toggled back to initial state
            mock_query.edit_message_reply_markup.assert_called_once()
            new_markup = mock_query.edit_message_reply_markup.call_args[1]["reply_markup"]
            assert new_markup.inline_keyboard[0][0].text == "🕒 Watch Later"
            assert new_markup.inline_keyboard[0][0].callback_data == "wl:test_vid_1"



@pytest.mark.asyncio
async def test_callback_playlist_action_unauthenticated():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")

        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.google_client_id = "test-cid"
        params.google_client_secret = "test-csec"

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        from telegram.ext import CallbackQueryHandler
        playlist_handler = next(
            h for h in registered_handlers
            if isinstance(h, CallbackQueryHandler) and getattr(h, "pattern", None) and "wl" in h.pattern.pattern
        )

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_query = AsyncMock()
        mock_query.data = "wl:test_vid_1"
        mock_update.callback_query = mock_query
        context = MagicMock()

        await playlist_handler.callback(mock_update, context)
        mock_query.answer.assert_called_with(
            "Please connect your YouTube account with /start first.",
            show_alert=True
        )


@pytest.mark.asyncio
async def test_cmd_custom_list_and_manage():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        custom_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "custom" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html
        mock_reply_text = AsyncMock()
        mock_update.effective_message.reply_text = mock_reply_text

        context = MagicMock()

        # 1. /custom with no feeds
        context.args = []
        await custom_handler.callback(mock_update, context)
        assert "no custom feeds configured" in mock_reply_html.call_args[0][0].lower()

        # 2. /custom add UC_x5XG1OV2P6uZZ5FSM9Ttw
        sample_feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <author><name>Google Developers</name></author>
  <entry><yt:videoId>v1</yt:videoId><title>Title 1</title></entry>
</feed>"""
        context.args = ["add", "UC_x5XG1OV2P6uZZ5FSM9Ttw"]
        with patch("handlers.fetch_feed_url", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = sample_feed
            await custom_handler.callback(mock_update, context)
            assert "Added custom feed: <b>Google Developers</b>" in mock_reply_html.call_args[0][0]

        # Check state
        feeds = sm.get_user_custom_feeds(1001)
        assert len(feeds) == 1
        expected_url = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"
        assert expected_url in feeds
        assert feeds[expected_url]["title"] == "Google Developers"

        # 3. /custom list
        context.args = ["list"]
        await custom_handler.callback(mock_update, context)
        reply = mock_reply_html.call_args[0][0]
        assert "1. <b>Google Developers</b>" in reply
        assert expected_url in reply

        # 4. /custom remove 1
        context.args = ["remove", "1"]
        await custom_handler.callback(mock_update, context)
        assert "Removed custom feed: <b>Google Developers</b>" in mock_reply_html.call_args[0][0]
        assert len(sm.get_user_custom_feeds(1001)) == 0

        # 5. /custom add with failing URL
        with patch("handlers.fetch_feed_url", new_callable=AsyncMock) as mock_fail_fetch:
            mock_fail_fetch.side_effect = Exception("HTTP 404")
            context.args = ["add", "https://broken.feed/rss"]
            await custom_handler.callback(mock_update, context)
            assert "Failed to fetch or parse feed from URL (HTTP 404)" in mock_reply_text.call_args[0][0]
            assert len(sm.get_user_custom_feeds(1001)) == 0


@pytest.mark.asyncio
async def test_cmd_custom_unauthorized():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        custom_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "custom" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 9999  # unauthorized
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html

        context = MagicMock()
        context.args = ["list"]
        await custom_handler.callback(mock_update, context)
        mock_reply_html.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.check_interval_sec = 300

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)
        setup_handlers(app, params, sm)

        status_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "status" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html
        context = MagicMock()

        # 1. No errors
        sm.sync_user_channels(1001, {"UC_1": "Channel 1"})
        await status_handler.callback(mock_update, context)
        reply = mock_reply_html.call_args[0][0]
        assert "Tracked channels: 1" in reply
        assert "Feeds with errors:" not in reply

        # 2. Feeds with errors (< 10)
        sm.record_feed_error(1001, is_custom=False, key="UC_1", error_msg="HTTP 500")
        sm.add_custom_feed(1001, "https://example.com/rss", "Custom Feed 1")
        sm.record_feed_error(1001, is_custom=True, key="https://example.com/rss", error_msg="HTTP 404")

        await status_handler.callback(mock_update, context)
        reply = mock_reply_html.call_args[0][0]
        assert "Feeds with errors:" in reply
        assert "• <b>Channel 1</b> (1 error: HTTP 500)" in reply
        assert "• <b>Custom Feed 1</b> (1 error: HTTP 404)" in reply
        assert "...and more" not in reply

        # 3. More than 10 feeds with errors
        channels = {f"UC_{i}": f"Channel {i}" for i in range(2, 15)}
        for ch_id, ch_title in channels.items():
            sm.get_user(1001)["channels"][ch_id] = {
                "title": ch_title,
                "error_count": 3,
                "last_error": "Connection timeout"
            }
        sm.save()

        await status_handler.callback(mock_update, context)
        reply = mock_reply_html.call_args[0][0]
        assert "Feeds with errors:" in reply
        # Should contain ...and more
        assert "...and more" in reply
        # Only 10 feed error items listed
        lines = [line for line in reply.split("\n") if line.startswith("• <b>")]
        assert len(lines) == 10


@pytest.mark.asyncio
async def test_apscheduler_job_duration_filter():
    from main import APSchedulerJobDurationFilter, format_duration

    # 1. Test format_duration
    assert format_duration(0) == "0s"
    assert format_duration(14.2) == "14s"
    assert format_duration(62.1) == "1m2s"
    assert format_duration(3665) == "1h1m5s"

    # 2. Test filter appending duration to apscheduler logs
    duration_filter = APSchedulerJobDurationFilter()
    job_str = 'rss_check (trigger: interval[0:05:00], next run at: 2026-09-13 15:02:47 UTC)'

    start_record = logging.LogRecord(
        name="apscheduler.executors.default",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='Running job "%s" (scheduled at %s)',
        args=(job_str, "2026-09-13 14:57:47.996090+00:00"),
        exc_info=None
    )
    assert duration_filter.filter(start_record) is True

    # Artificially set start time 62 seconds in past
    duration_filter.job_start_times[job_str] -= 62.0

    success_record = logging.LogRecord(
        name="apscheduler.executors.default",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='Job "%s" executed successfully',
        args=(job_str,),
        exc_info=None
    )
    assert duration_filter.filter(success_record) is True
    assert success_record.getMessage() == f'Job "{job_str}" executed successfully, took 1m2s'

    # Filter running a second time should not duplicate ", took"
    assert duration_filter.filter(success_record) is True
    assert success_record.getMessage() == f'Job "{job_str}" executed successfully, took 1m2s'


@pytest.mark.asyncio
async def test_cmd_stop():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        stop_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "stop" in h.commands)

        # 1. Unauthorized user
        mock_unauth = MagicMock()
        mock_unauth.effective_user.id = 9999
        mock_reply_unauth = AsyncMock()
        mock_unauth.effective_message.reply_html = mock_reply_unauth
        await stop_handler.callback(mock_unauth, MagicMock())
        mock_reply_unauth.assert_not_called()

        # 2. Authorized user with empty queue
        mock_user = MagicMock()
        mock_user.effective_user.id = 1001
        mock_reply_empty = AsyncMock()
        mock_user.effective_message.reply_html = mock_reply_empty
        await stop_handler.callback(mock_user, MagicMock())
        mock_reply_empty.assert_called_with("No pending notifications in queue.")

        # 3. Authorized user with 3 pending notifications
        for i in range(3):
            sm.enqueue_notification(1001, f"Video {i}", f"https://youtube.com/watch?v={i}")
        assert sm.get_pending_notifications_count(1001) == 3

        mock_reply_cleared = AsyncMock()
        mock_user.effective_message.reply_html = mock_reply_cleared
        await stop_handler.callback(mock_user, MagicMock())
        mock_reply_cleared.assert_called_with("Cleared 3 pending notifications.")
        assert sm.get_pending_notifications_count(1001) == 0


@pytest.mark.asyncio
async def test_cmd_status_shows_pending_notifications():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]

        app = MagicMock()
        registered_handlers = []
        app.add_handler = lambda h: registered_handlers.append(h)

        setup_handlers(app, params, sm)

        status_handler = next(h for h in registered_handlers if hasattr(h, "commands") and "status" in h.commands)

        mock_update = MagicMock()
        mock_update.effective_user.id = 1001
        mock_reply_html = AsyncMock()
        mock_update.effective_message.reply_html = mock_reply_html
        context = MagicMock()

        # Without pending notifications
        await status_handler.callback(mock_update, context)
        reply1 = mock_reply_html.call_args[0][0]
        assert "Pending notifications" not in reply1

        # With 4 pending notifications
        for i in range(4):
            sm.enqueue_notification(1001, f"Vid {i}", f"https://youtube.com/watch?v={i}")

        await status_handler.callback(mock_update, context)
        reply2 = mock_reply_html.call_args[0][0]
        assert "• Pending notifications: 4" in reply2





