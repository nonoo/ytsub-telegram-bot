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


