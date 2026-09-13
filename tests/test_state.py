import json
import os
import tempfile
import pytest
from state import StateManager, format_human_timestamp


def test_state_multi_user_and_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)
        sm.load()

        # Set user 1 tokens
        sm.set_user_tokens(1001, "tok-1", "ref-1")

        # Set user 2 tokens
        sm.set_user_tokens(2002, "tok-2", "ref-2")

        assert sm.has_user_oauth_credentials(1001)
        assert sm.has_user_oauth_credentials(2002)
        assert not sm.has_user_oauth_credentials(3003)

        # Sync subscriptions for user 1
        new_cnt1 = sm.sync_user_channels(1001, {"UC_A": "Channel A", "UC_B": "Channel B"})
        assert new_cnt1 == 2

        # Sync subscriptions for user 2 with different channels
        new_cnt2 = sm.sync_user_channels(2002, {"UC_B": "Channel B", "UC_C": "Channel C"})
        assert new_cnt2 == 2

        # Check channel info in user 1
        u1_channels = sm.get_user(1001)["channels"]
        assert "UC_A" in u1_channels
        assert "UC_B" in u1_channels
        assert "UC_C" not in u1_channels
        assert "last_published_human" not in u1_channels["UC_A"]
        assert "UTC" in u1_channels["UC_A"]["last_published"]
        assert "last_checked_human" not in u1_channels["UC_A"]
        assert "UTC" in u1_channels["UC_A"]["last_checked"]

        # Preserve timestamps on subsequent sync
        original_last_pub = u1_channels["UC_A"]["last_published"]
        new_cnt_resync = sm.sync_user_channels(1001, {"UC_A": "Channel A Renamed", "UC_D": "Channel D"})
        assert new_cnt_resync == 1  # UC_D is new
        u1_channels_after = sm.get_user(1001)["channels"]
        assert u1_channels_after["UC_A"]["last_published"] == original_last_pub
        assert u1_channels_after["UC_A"]["title"] == "Channel A Renamed"
        assert "UC_B" not in u1_channels_after  # Unsubscribed

        # Reload from disk into a fresh instance
        sm2 = StateManager(state_file)
        sm2.load()
        assert sm2.has_user_oauth_credentials(1001)
        assert sm2.has_user_oauth_credentials(2002)
        assert "UC_C" in sm2.get_user(2002)["channels"]
        assert "client_id" not in sm2.get_user(1001)
        assert "client_secret" not in sm2.get_user(1001)
        assert "chat_id" not in sm2.get_user(1001)


def test_state_removes_legacy_client_credentials():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-legacy.json")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({
                "users": {
                    "123": {
                        "chat_id": 123,
                        "client_id": "old-id",
                        "client_secret": "old-secret",
                        "token": "tok",
                        "refresh_token": "ref",
                        "channels": {}
                    }
                }
            }, f)
        sm = StateManager(state_file)
        sm.load()
        u = sm.get_user(123)
        assert "client_id" not in u
        assert "client_secret" not in u
        assert "chat_id" not in u


def test_update_channel_timestamps():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)
        sm.sync_user_channels(1001, {"UC_A": "Channel A"})

        sm.update_channel_timestamps(
            1001,
            "UC_A",
            last_published_iso="2026-09-13T10:00:00+00:00",
            last_checked_epoch=1789377164.0
        )

        ch = sm.get_user(1001)["channels"]["UC_A"]
        assert ch["last_published"] == "2026-09-13 10:00:00 UTC"
        assert "last_published_human" not in ch
        assert "UTC" in ch["last_checked"]
        assert "last_checked_human" not in ch
