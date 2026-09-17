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


def test_user_playlist_caching():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)

        assert sm.get_user_playlist(1001, "watch_later") is None
        sm.set_user_playlist(1001, "watch_later", "PL_wl_123")
        assert sm.get_user_playlist(1001, "watch_later") == "PL_wl_123"

        # Check persistence across reload
        sm2 = StateManager(state_file)
        sm2.load()
        assert sm2.get_user_playlist(1001, "watch_later") == "PL_wl_123"

        sm2.clear_user_playlist(1001, "watch_later")
        assert sm2.get_user_playlist(1001, "watch_later") is None


def test_custom_feeds_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)

        feed_url1 = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_TEST1"
        feed_url2 = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_TEST2"

        # Add feeds
        assert sm.add_custom_feed(1001, feed_url1, "Channel 1") is True
        assert sm.add_custom_feed(1001, feed_url2, "Channel 2") is True
        # Re-adding returns False
        assert sm.add_custom_feed(1001, feed_url1, "Channel 1 Updated") is False

        feeds = sm.get_user_custom_feeds(1001)
        assert len(feeds) == 2
        assert feeds[feed_url1]["title"] == "Channel 1 Updated"
        assert "UTC" in feeds[feed_url1]["last_published"]
        assert "UTC" in feeds[feed_url1]["last_checked"]

        # Update timestamps
        sm.update_custom_feed_timestamps(
            1001,
            feed_url1,
            last_published="2026-09-13 12:00:00 UTC",
            last_checked_epoch=1789377164.0
        )
        assert feeds[feed_url1]["last_published"] == "2026-09-13 12:00:00 UTC"

        # Remove by index "1"
        removed1 = sm.remove_custom_feed(1001, "1")
        assert removed1 is not None
        assert removed1["url"] == feed_url1
        assert len(sm.get_user_custom_feeds(1001)) == 1

        # Remove by URL
        removed2 = sm.remove_custom_feed(1001, feed_url2)
        assert removed2 is not None
        assert removed2["url"] == feed_url2
        assert len(sm.get_user_custom_feeds(1001)) == 0

        # Remove non-existent returns None
        assert sm.remove_custom_feed(1001, "999") is None


def test_sync_preserves_custom_feeds():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)

        feed_url = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_CUSTOM"
        sm.add_custom_feed(1001, feed_url, "Custom Channel")

        # Sync subscriptions from YouTube
        sm.sync_user_channels(1001, {"UC_A": "Channel A"})

        # Verify custom feeds are not deleted by sync_user_channels
        feeds = sm.get_user_custom_feeds(1001)
        assert feed_url in feeds
        assert feeds[feed_url]["title"] == "Custom Channel"

        # Verify channels are updated properly
        assert "UC_A" in sm.get_user(1001)["channels"]


def test_record_and_reset_feed_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)

        # 1. Test regular channel
        sm.sync_user_channels(1001, {"UC_A": "Channel A"})
        ch = sm.get_user(1001)["channels"]["UC_A"]
        assert ch["error_count"] == 0
        assert ch["last_error"] is None

        # Failures 1 through 49 should return False (no alert)
        for i in range(1, 50):
            alert = sm.record_feed_error(1001, is_custom=False, key="UC_A", error_msg=f"Err {i}")
            assert alert is False
            assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == i
            assert sm.get_user(1001)["channels"]["UC_A"]["last_error"] == f"Err {i}"

        # 50th failure should return True (trigger alert)
        alert = sm.record_feed_error(1001, is_custom=False, key="UC_A", error_msg="Err 50")
        assert alert is True
        assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == 50

        # 51st failure should increment count to 51 and return False (alert not re-triggered)
        alert = sm.record_feed_error(1001, is_custom=False, key="UC_A", error_msg="Err 51")
        assert alert is False
        assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == 51
        assert sm.get_user(1001)["channels"]["UC_A"]["last_error"] == "Err 51"

        # Error count should cap at INT64_MAX
        sm.get_user(1001)["channels"]["UC_A"]["error_count"] = (1 << 63) - 1
        alert = sm.record_feed_error(1001, is_custom=False, key="UC_A", error_msg="Err max")
        assert alert is False
        assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == (1 << 63) - 1

        # Successful reset after reaching 50 should return True (trigger recovery alert)
        recovered = sm.reset_feed_error(1001, is_custom=False, key="UC_A")
        assert recovered is True
        ch_after = sm.get_user(1001)["channels"]["UC_A"]
        assert ch_after["error_count"] == 0
        assert ch_after["last_error"] is None

        # Resetting again when count is 0 returns False
        assert sm.reset_feed_error(1001, is_custom=False, key="UC_A") is False

        # Resetting when count was < 50 returns False
        sm.record_feed_error(1001, is_custom=False, key="UC_A", error_msg="Minor err")
        assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == 1
        assert sm.reset_feed_error(1001, is_custom=False, key="UC_A") is False
        assert sm.get_user(1001)["channels"]["UC_A"]["error_count"] == 0

        # 2. Test custom feed
        feed_url = "https://example.com/rss.xml"
        sm.add_custom_feed(1001, feed_url, "Custom Feed")
        feed = sm.get_user_custom_feeds(1001)[feed_url]
        assert feed["error_count"] == 0
        assert feed["last_error"] is None

        for _ in range(49):
            sm.record_feed_error(1001, is_custom=True, key=feed_url, error_msg="Err")
        alert = sm.record_feed_error(1001, is_custom=True, key=feed_url, error_msg="Err 50")
        assert alert is True

        recovered = sm.reset_feed_error(1001, is_custom=True, key=feed_url)
        assert recovered is True
        assert sm.get_user_custom_feeds(1001)[feed_url]["error_count"] == 0
        assert sm.get_user_custom_feeds(1001)[feed_url]["last_error"] is None


def test_pending_notifications_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = os.path.join(tmpdir, "test-state.json")
        sm = StateManager(state_file)

        # Initially empty
        assert sm.get_pending_notifications(1001) == []
        assert sm.peek_pending_notification(1001) is None
        assert sm.pop_pending_notification(1001) is None
        assert sm.get_pending_notifications_count(1001) == 0
        assert sm.get_users_with_pending_notifications() == []

        # Enqueue items
        assert sm.enqueue_notification(
            user_id=1001,
            title="Channel 1",
            url="https://youtube.com/watch?v=vid1",
            video_id="vid1",
            published="2026-09-13T10:00:00+00:00"
        ) is True

        # Deduplication check: same URL should return False
        assert sm.enqueue_notification(
            user_id=1001,
            title="Channel 1 (duplicate)",
            url="https://youtube.com/watch?v=vid1",
            video_id="vid1",
            published="2026-09-13T10:00:00+00:00"
        ) is False

        # Enqueue second item for user 1001
        assert sm.enqueue_notification(
            user_id=1001,
            title="Channel 2",
            url="https://youtube.com/watch?v=vid2",
            video_id="vid2",
            published="2026-09-13T11:00:00+00:00"
        ) is True

        # Enqueue item for user 2002
        assert sm.enqueue_notification(
            user_id=2002,
            title="Channel 3",
            url="https://youtube.com/watch?v=vid3",
            video_id="vid3",
            published="2026-09-13T12:00:00+00:00"
        ) is True

        assert sm.get_pending_notifications_count(1001) == 2
        assert sm.get_pending_notifications_count(2002) == 1
        assert sm.get_pending_notifications_count() == 3
        assert set(sm.get_users_with_pending_notifications()) == {1001, 2002}

        # Verify peek does not remove item
        peeked = sm.peek_pending_notification(1001)
        assert peeked is not None
        assert peeked["video_id"] == "vid1"
        assert sm.get_pending_notifications_count(1001) == 2

        # Verify reload from disk persists queue
        sm2 = StateManager(state_file)
        sm2.load()
        assert sm2.get_pending_notifications_count(1001) == 2
        assert sm2.get_pending_notifications_count(2002) == 1

        # Pop item
        popped = sm2.pop_pending_notification(1001)
        assert popped is not None
        assert popped["video_id"] == "vid1"
        assert sm2.get_pending_notifications_count(1001) == 1
        assert sm2.peek_pending_notification(1001)["video_id"] == "vid2"

        # Clear notifications
        cleared = sm2.clear_pending_notifications(1001)
        assert cleared == 1
        assert sm2.get_pending_notifications_count(1001) == 0
        assert sm2.get_users_with_pending_notifications() == [2002]

        # Clearing again returns 0
        assert sm2.clear_pending_notifications(1001) == 0


def test_state_manager_type_hints():
    import typing
    for attr_name in dir(StateManager):
        attr = getattr(StateManager, attr_name)
        if callable(attr):
            typing.get_type_hints(attr)
