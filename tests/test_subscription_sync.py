import asyncio
import tempfile
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from params import Params
from state import StateManager
from youtube import (
    format_channel_delta_log,
    sync_all_subscriptions,
    sync_user_subscriptions,
)


@pytest.mark.asyncio
async def test_sync_user_subscriptions_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.google_client_id = "test-client-id"
        params.google_client_secret = "test-client-secret"

        sm.set_user_tokens(1001, token="token-1", refresh_token="refresh-1")

        with patch("youtube.fetch_user_subscriptions") as mock_fetch:
            mock_fetch.return_value = ({"UC_1": "Channel 1", "UC_2": "Channel 2"}, "token-refreshed")

            total, new_count = await sync_user_subscriptions(sm, params, 1001)

            assert total == 2
            assert new_count == 2
            mock_fetch.assert_called_once_with("test-client-id", "test-client-secret", "token-1", "refresh-1")

            user = sm.get_user(1001)
            assert user["token"] == "token-refreshed"
            assert "UC_1" in user["channels"]
            assert "UC_2" in user["channels"]


@pytest.mark.asyncio
async def test_sync_user_subscriptions_unauthenticated():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.google_client_id = "test-client-id"
        params.google_client_secret = "test-client-secret"

        with pytest.raises(ValueError, match="does not have OAuth credentials"):
            await sync_user_subscriptions(sm, params, 1001)


@pytest.mark.asyncio
async def test_sync_preserves_existing_channel_metadata():
    """
    Ensure running subscription sync (periodic or /update) does not reset
    last_published, last_checked, first_error, last_error, error_alerted for channels that remain in state.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = f"{tmpdir}/state.json"
        sm = StateManager(state_file)
        params = Params()
        params.google_client_id = "test-client-id"
        params.google_client_secret = "test-client-secret"

        sm.set_user_tokens(1001, token="tok", refresh_token="ref")

        # Setup pre-existing channels with custom timestamps and error state
        user = sm.get_user(1001)
        user["channels"] = {
            "UC_STAYS": {
                "title": "Old Name",
                "last_published": "2026-01-01 10:00:00 UTC",
                "last_checked": "2026-01-01 10:05:00 UTC",
                "first_error": "2026-01-01 10:05:00 UTC",
                "last_error": "503 Service Unavailable",
                "error_alerted": True
            },
            "UC_LEAVING": {
                "title": "Unsubscribed Channel",
                "last_published": "2026-01-01 08:00:00 UTC",
                "last_checked": "2026-01-01 08:05:00 UTC",
                "first_error": None,
                "last_error": None,
                "error_alerted": False
            }
        }
        sm.save()

        # YouTube API returns UC_STAYS (with updated title) and a brand new UC_NEW
        with patch("youtube.fetch_user_subscriptions") as mock_fetch:
            mock_fetch.return_value = (
                {
                    "UC_STAYS": "New Name",
                    "UC_NEW": "Brand New Channel"
                },
                None
            )

            total, new_count = await sync_user_subscriptions(sm, params, 1001)

            assert total == 2
            assert new_count == 1  # Only UC_NEW is new

            channels_after = sm.get_user(1001)["channels"]

            # UC_STAYS must preserve all metadata, only title updated
            staying_ch = channels_after["UC_STAYS"]
            assert staying_ch["title"] == "New Name"
            assert staying_ch["last_published"] == "2026-01-01 10:00:00 UTC"
            assert staying_ch["last_checked"] == "2026-01-01 10:05:00 UTC"
            assert staying_ch.get("error_count") is None
            assert staying_ch["first_error"] == "2026-01-01 10:05:00 UTC"
            assert staying_ch["last_error"] == "503 Service Unavailable"
            assert staying_ch["error_alerted"] is True

            # UC_LEAVING must be removed
            assert "UC_LEAVING" not in channels_after

            # UC_NEW must be initialized fresh
            new_ch = channels_after["UC_NEW"]
            assert new_ch["title"] == "Brand New Channel"
            assert new_ch.get("error_count") is None
            assert new_ch["first_error"] is None
            assert new_ch["last_error"] is None
            assert new_ch["error_alerted"] is False
            assert "UTC" in new_ch["last_published"]
            assert "UTC" in new_ch["last_checked"]

            # Reload from disk to guarantee persistence
            sm2 = StateManager(state_file)
            sm2.load()
            persisted_staying = sm2.get_user(1001)["channels"]["UC_STAYS"]
            assert persisted_staying["last_published"] == "2026-01-01 10:00:00 UTC"
            assert persisted_staying["last_checked"] == "2026-01-01 10:05:00 UTC"
            assert persisted_staying.get("error_count") is None
            assert persisted_staying["first_error"] == "2026-01-01 10:05:00 UTC"
            assert persisted_staying["last_error"] == "503 Service Unavailable"
            assert persisted_staying["error_alerted"] is True


@pytest.mark.asyncio
async def test_sync_all_subscriptions_filtering_and_error_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.google_client_id = "test-client-id"
        params.google_client_secret = "test-client-secret"
        params.allowed_user_ids = [1001, 1002, 1003]

        # 1001: Allowed and authenticated
        sm.set_user_tokens(1001, token="t1", refresh_token="r1")
        # 1002: Allowed and authenticated, but will raise an exception during fetch
        sm.set_user_tokens(1002, token="t2", refresh_token="r2")
        # 1003: Allowed but not authenticated
        sm.get_user(1003)
        # 1004: Not allowed, even though it has credentials
        sm.set_user_tokens(1004, token="t4", refresh_token="r4")

        with patch("youtube.fetch_user_subscriptions") as mock_fetch:
            def side_effect(cid, csec, tok, rtok):
                if tok == "t1":
                    return ({"UC_A": "Channel A"}, None)
                if tok == "t2":
                    raise RuntimeError("API error")
                return ({}, None)

            mock_fetch.side_effect = side_effect

            results = await sync_all_subscriptions(sm, params)

            assert 1001 in results
            assert results[1001] == (1, 1)
            assert 1002 not in results
            assert 1003 not in results
            assert 1004 not in results


@pytest.mark.asyncio
async def test_sync_all_subscriptions_missing_google_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.google_client_id = ""
        params.google_client_secret = ""

        sm.set_user_tokens(1001, token="t1", refresh_token="r1")

        with patch("youtube.fetch_user_subscriptions") as mock_fetch:
            results = await sync_all_subscriptions(sm, params)
            assert results == {}
            mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_subscription_sync_job():
    from main import scheduled_subscription_sync

    assert Params().subscription_sync_interval_sec == 12 * 3600

    context = MagicMock()
    sm = MagicMock()
    params = MagicMock()
    context.job.data = {"state": sm, "params": params}

    with patch("main.sync_all_subscriptions", new_callable=AsyncMock) as mock_sync:
        await scheduled_subscription_sync(context)
        mock_sync.assert_called_once_with(state=sm, params=params, send_message_fn=ANY)


@pytest.mark.asyncio
async def test_cmd_update_delegates_to_sync_user_subscriptions():
    from handlers import setup_handlers

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.bot_token = "123:test"
        params.allowed_user_ids = [1001]
        params.google_client_id = "test-cid"
        params.google_client_secret = "test-csec"

        sm.set_user_tokens(1001, token="t1", refresh_token="r1")

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

        with patch("youtube.sync_user_subscriptions", new_callable=AsyncMock, return_value=(5, 2)) as mock_sync:
            await update_handler.callback(mock_update, context)

            mock_sync.assert_called_once_with(sm, params, 1001, send_message_fn=ANY)
            assert mock_reply.call_count == 2
            mock_reply.assert_any_call("🔄 Redownloading subscribed channels...")
            assert "Successfully synced subscriptions." in mock_reply.call_args_list[1][0][0]
            assert "Total channels tracked: 5 (2 newly added)." in mock_reply.call_args_list[1][0][0]


def test_format_channel_delta_log():
    # Both added and removed <= 10
    msg = format_channel_delta_log(["Ch 1", "Ch 2"], ["Ch 3", "Ch 4"])
    assert msg == "➕ Added channels: Ch 1, Ch 2\n➖ Removed channels: Ch 3, Ch 4"

    # More than 10 channels added
    added_12 = [f"Add {i}" for i in range(1, 13)]
    msg = format_channel_delta_log(added_12, ["Rem 1"])
    expected_added = ", ".join(f"Add {i}" for i in range(1, 11)) + ", and 2 more"
    assert msg == f"➕ Added channels: {expected_added}\n➖ Removed channels: Rem 1"

    # More than 10 channels removed
    rem_12 = [f"Rem {i}" for i in range(1, 13)]
    msg = format_channel_delta_log(["Add 1"], rem_12)
    expected_rem = ", ".join(f"Rem {i}" for i in range(1, 11)) + ", and 2 more"
    assert msg == f"➕ Added channels: Add 1\n➖ Removed channels: {expected_rem}"

    # Only added
    msg = format_channel_delta_log(["Add 1"], [])
    assert msg == "➕ Added channels: Add 1"

    # Only removed
    msg = format_channel_delta_log([], ["Rem 1"])
    assert msg == "➖ Removed channels: Rem 1"

    # None added or removed
    assert format_channel_delta_log([], []) is None


@pytest.mark.asyncio
async def test_sync_user_subscriptions_delta_logging():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        params = Params()
        params.google_client_id = "test-client-id"
        params.google_client_secret = "test-client-secret"

        sm.set_user_tokens(1001, token="tok", refresh_token="ref")
        # Prepopulate with UC_1 and UC_2
        user = sm.get_user(1001)
        user["channels"] = {
            "UC_1": {"title": "Channel 1"},
            "UC_2": {"title": "Channel 2"}
        }
        sm.save()

        with patch("youtube.fetch_user_subscriptions") as mock_fetch, \
             patch("youtube.logger.info") as mock_logger_info:

            # After sync: UC_1 stays, UC_2 is removed, UC_3 is added
            mock_fetch.return_value = ({"UC_1": "Channel 1", "UC_3": "Channel 3"}, None)

            mock_send = AsyncMock()
            await sync_user_subscriptions(sm, params, 1001, send_message_fn=mock_send)

            # Check that logger.info was called with Added and Removed channels
            expected_log = "➕ Added channels: Channel 3\n➖ Removed channels: Channel 2"
            mock_logger_info.assert_any_call("%s", expected_log)
            mock_send.assert_called_once_with(1001, expected_log)

        # Resync with identical channels: no channels added or removed
        with patch("youtube.fetch_user_subscriptions") as mock_fetch, \
             patch("youtube.logger.info") as mock_logger_info:

            mock_fetch.return_value = ({"UC_1": "Channel 1", "UC_3": "Channel 3"}, None)

            mock_send = AsyncMock()
            await sync_user_subscriptions(sm, params, 1001, send_message_fn=mock_send)

            # logger.info and send_message_fn should not have been called with Added or Removed channels
            mock_send.assert_not_called()
            for call in mock_logger_info.call_args_list:
                if call[0]:
                    assert "Added channels:" not in str(call[0][0])
                    assert "Removed channels:" not in str(call[0][0])

        # Test graceful handling if send_message_fn raises exception
        with patch("youtube.fetch_user_subscriptions") as mock_fetch:
            mock_fetch.return_value = ({"UC_1": "Channel 1"}, None)
            failing_send = AsyncMock(side_effect=RuntimeError("Network error"))
            # Should not raise exception
            total, new_cnt = await sync_user_subscriptions(sm, params, 1001, send_message_fn=failing_send)
            assert total == 1
            failing_send.assert_called_once()


