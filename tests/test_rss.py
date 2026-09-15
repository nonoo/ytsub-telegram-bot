import asyncio
import logging
import re
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest
from rss import FeedFetchError, VideoEntry, check_channels_and_notify, parse_atom_feed
from state import StateManager

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <title>Sample Channel</title>
  <entry>
    <id>yt:video:vid111</id>
    <yt:videoId>vid111</yt:videoId>
    <yt:channelId>UC_X</yt:channelId>
    <title>First Video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid111"/>
    <published>2026-09-13T08:00:00+00:00</published>
    <updated>2026-09-13T08:00:00+00:00</updated>
  </entry>
  <entry>
    <id>yt:video:vid222</id>
    <yt:videoId>vid222</yt:videoId>
    <yt:channelId>UC_X</yt:channelId>
    <title>Second Video</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=vid222"/>
    <published>2026-09-13T09:00:00+00:00</published>
    <updated>2026-09-13T09:00:00+00:00</updated>
  </entry>
</feed>
"""


def test_parse_atom_feed():
    entries = parse_atom_feed(SAMPLE_FEED)
    assert len(entries) == 2
    assert entries[0].video_id == "vid111"
    assert entries[0].url == "https://www.youtube.com/watch?v=vid111"
    assert entries[1].video_id == "vid222"
    assert entries[1].url == "https://www.youtube.com/watch?v=vid222"
    # Ensure sorted oldest first
    assert entries[0].published_dt < entries[1].published_dt


@pytest.mark.asyncio
async def test_check_channels_and_notify_user_isolation(caplog):
    caplog.set_level(logging.INFO)
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")

        # Setup User 1 (subscribed to UC_X)
        sm.set_user_tokens(101, "t1", "r1")
        sm.sync_user_channels(101, {"UC_X": "Channel X"})
        # Set last_published to before vid222
        sm.update_channel_timestamps(101, "UC_X", last_published_iso="2026-09-13T08:30:00+00:00")

        # Setup User 2 (subscribed to UC_Y, NOT UC_X)
        sm.set_user_tokens(202, "t2", "r2")
        sm.sync_user_channels(202, {"UC_Y": "Channel Y"})

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text, reply_markup))

        with patch("rss.fetch_channel_rss", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_FEED

            checked = await check_channels_and_notify(
                state=sm,
                send_message_fn=mock_send,
                auto_dispatch=True
            )

            assert checked > 0
            # User 1 should get notified about vid222, but NOT vid111
            assert len(sent_messages) == 1
            chat_id, text, reply_markup = sent_messages[0]
            assert chat_id == 101
            assert text.startswith("<b>Channel X</b> https://www.youtube.com/watch?v=vid222")
            assert "ago)" in text
            assert reply_markup is not None
            buttons = reply_markup.inline_keyboard[0]
            assert len(buttons) == 2
            assert buttons[0].text == "🕒 Watch Later"
            assert buttons[0].callback_data == "wl:vid222"
            assert buttons[1].text == "🎧 Listen Later"
            assert buttons[1].callback_data == "ll:vid222"

            # Check that User 1's last_published was updated to vid222's timestamp
            u1_ch = sm.get_user(101)["channels"]["UC_X"]
            assert u1_ch["last_published"] == "2026-09-13 09:00:00 UTC"
            assert "last_published_human" not in u1_ch

            # Check that RSS feed update was logged
            assert "RSS update found for 'Channel X'" in caplog.text


@pytest.mark.asyncio
async def test_min_seconds_since_check_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.set_user_tokens(101, "t1", "r1")
        sm.sync_user_channels(101, {"UC_X": "Channel X"})

        # Mark as checked 60 seconds ago
        sm.update_channel_timestamps(101, "UC_X", last_checked_epoch=time.time() - 60)

        sent_messages = []

        async def mock_send(chat_id: int, text: str):
            sent_messages.append((chat_id, text))

        with patch("rss.fetch_channel_rss", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_FEED

            # With min_seconds_since_check=300 (5 minutes), should skip channel checked 60s ago
            checked = await check_channels_and_notify(
                state=sm,
                send_message_fn=mock_send,
                min_seconds_since_check=300.0
            )
            assert checked == 0
            assert len(sent_messages) == 0

            # Mark as checked 350 seconds ago (> 5 minutes)
            sm.update_channel_timestamps(101, "UC_X", last_checked_epoch=time.time() - 350)
            checked = await check_channels_and_notify(
                state=sm,
                send_message_fn=mock_send,
                min_seconds_since_check=300.0
            )
            assert checked == 1


def test_normalize_feed_url():
    from rss import normalize_feed_url

    # Channel ID
    assert normalize_feed_url("UC_x5XG1OV2P6uZZ5FSM9Ttw") == "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"

    # YouTube Channel URL
    assert normalize_feed_url("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw") == "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"

    # YouTube Playlist URL
    assert normalize_feed_url("https://www.youtube.com/playlist?list=PL1234567890abcdef") == "https://www.youtube.com/feeds/videos.xml?playlist_id=PL1234567890abcdef"

    # Arbitrary feed URL
    assert normalize_feed_url("https://example.com/custom.xml") == "https://example.com/custom.xml"


def test_parse_feed_with_author():
    from rss import parse_feed

    atom_with_author = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <title>Feed Title Fallback</title>
  <author>
    <name>Channel Author Name</name>
    <uri>https://www.youtube.com/channel/UC_X</uri>
  </author>
  <entry>
    <id>yt:video:vid333</id>
    <yt:videoId>vid333</yt:videoId>
    <title>Video 3</title>
    <published>2026-09-13T10:00:00+00:00</published>
  </entry>
</feed>"""

    author, entries = parse_feed(atom_with_author)
    assert author == "Channel Author Name"
    assert len(entries) == 1
    assert entries[0].video_id == "vid333"


@pytest.mark.asyncio
async def test_check_custom_feeds_and_notify():
    from rss import check_channels_and_notify

    custom_feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <author>
    <name>Custom Author</name>
  </author>
  <entry>
    <id>yt:video:cust1</id>
    <yt:videoId>cust1</yt:videoId>
    <title>Custom Video 1</title>
    <published>2026-09-13T10:00:00+00:00</published>
  </entry>
</feed>"""

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        feed_url = "https://custom.feed/rss.xml"
        sm.add_custom_feed(101, feed_url, "Custom Author")
        # Set last_published to before the video
        sm.update_custom_feed_timestamps(101, feed_url, last_published="2026-09-13 09:00:00 UTC")

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text, reply_markup))

        with patch("rss.fetch_feed_url", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = custom_feed_xml

            checked = await check_channels_and_notify(
                state=sm,
                send_message_fn=mock_send,
                auto_dispatch=True
            )

            assert checked == 1
            assert len(sent_messages) == 1
            chat_id, text, reply_markup = sent_messages[0]
            assert chat_id == 101
            assert text.startswith("<b>Custom Author</b> https://www.youtube.com/watch?v=cust1")
            assert "ago)" in text
            assert reply_markup is not None
            buttons = reply_markup.inline_keyboard[0]
            assert buttons[0].callback_data == "wl:cust1"
            assert buttons[1].callback_data == "ll:cust1"


@pytest.mark.asyncio
async def test_feed_error_alert_on_tenth_failure_and_recovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.set_user_tokens(101, "t1", "r1")
        sm.sync_user_channels(101, {"UC_ERR": "Broken Channel"})

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text))

        with patch("rss.fetch_channel_rss", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = FeedFetchError("HTTP 500")

            # Run 9 failures: error_count goes 1..9, no alert messages sent
            for i in range(1, 10):
                checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
                assert checked == 0
                assert len(sent_messages) == 0
                ch = sm.get_user(101)["channels"]["UC_ERR"]
                assert ch["error_count"] == i
                assert ch["last_error"] == "HTTP 500"

            # 10th failure: alert message must be sent to user
            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 0
            assert len(sent_messages) == 1
            chat_id, text = sent_messages[0]
            assert chat_id == 101
            assert text == "Error updating: Broken Channel"

            ch = sm.get_user(101)["channels"]["UC_ERR"]
            assert ch["error_count"] == 10

            # 11th failure: error_count increments to 11, no new alert sent
            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 0
            assert len(sent_messages) == 1
            assert sm.get_user(101)["channels"]["UC_ERR"]["error_count"] == 11

            # 12th run: feed recovers!
            mock_fetch.side_effect = None
            mock_fetch.return_value = SAMPLE_FEED

            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 1
            # A recovery notification should have been sent
            assert len(sent_messages) == 2
            chat_id, text = sent_messages[1]
            assert chat_id == 101
            assert text == "Working again: Broken Channel"

            # State error_count should be reset to 0 and last_error to None
            ch = sm.get_user(101)["channels"]["UC_ERR"]
            assert ch["error_count"] == 0
            assert ch["last_error"] is None


@pytest.mark.asyncio
async def test_feed_error_alert_batching_and_overflow():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.get_user(101)["token"] = "valid_token"

        # Create 12 channels for user 101, each already with 9 errors
        channels = {}
        for i in range(1, 13):
            ch_id = f"UC_{i:02d}"
            channels[ch_id] = f"Channel {i}"
        sm.sync_user_channels(101, channels)

        for ch_id in channels:
            sm.get_user(101)["channels"][ch_id]["error_count"] = 9

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text))

        with patch("rss.fetch_channel_rss", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = FeedFetchError("HTTP 500")

            # 10th failure for all 12 channels -> exactly ONE batched message sent with first 10 and '... and more'
            await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert len(sent_messages) == 1
            chat_id, text = sent_messages[0]
            assert chat_id == 101
            expected_prefix = "Error updating: "
            assert text.startswith(expected_prefix)
            assert text.endswith(", ... and more")
            # 10 channel names included
            feed_names = text[len(expected_prefix):-len(", ... and more")].split(", ")
            assert len(feed_names) == 10

            # Now all 12 channels recover -> exactly ONE batched recovery message sent with first 10 and '... and more'
            sent_messages.clear()
            mock_fetch.side_effect = None
            mock_fetch.return_value = SAMPLE_FEED

            await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert len(sent_messages) == 1
            chat_id, text = sent_messages[0]
            assert chat_id == 101
            expected_rec_prefix = "Working again: "
            assert text.startswith(expected_rec_prefix)
            assert text.endswith(", ... and more")
            rec_names = text[len(expected_rec_prefix):-len(", ... and more")].split(", ")
            assert len(rec_names) == 10



@pytest.mark.asyncio
async def test_feed_parse_error_tracking():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")
        sm.add_custom_feed(101, "https://example.com/bad.xml", "Malformed Feed")

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text))

        with patch("rss.fetch_feed_url", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = "<html><body>Not XML</body></html>"

            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 0
            feed = sm.get_user_custom_feeds(101)["https://example.com/bad.xml"]
            assert feed["error_count"] == 1
            assert "Invalid feed format" in feed["last_error"]


def test_format_time_ago():
    from rss import format_time_ago
    from datetime import datetime, timezone, timedelta

    base = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

    # < 1 min -> Xs ago
    assert format_time_ago(base - timedelta(seconds=0), now_dt=base) == "0s ago"
    assert format_time_ago(base - timedelta(seconds=5), now_dt=base) == "5s ago"
    assert format_time_ago(base - timedelta(seconds=59), now_dt=base) == "59s ago"

    # 1 min to 59 mins -> Xm ago
    assert format_time_ago(base - timedelta(seconds=60), now_dt=base) == "1m ago"
    assert format_time_ago(base - timedelta(minutes=5), now_dt=base) == "5m ago"
    assert format_time_ago(base - timedelta(minutes=59, seconds=59), now_dt=base) == "59m ago"

    # 60 mins to 23 hours -> Xh ago
    assert format_time_ago(base - timedelta(hours=1), now_dt=base) == "1h ago"
    assert format_time_ago(base - timedelta(hours=5), now_dt=base) == "5h ago"
    assert format_time_ago(base - timedelta(hours=23, minutes=59), now_dt=base) == "23h ago"

    # >= 24 hours -> Xd ago
    assert format_time_ago(base - timedelta(days=1), now_dt=base) == "1d ago"
    assert format_time_ago(base - timedelta(days=5), now_dt=base) == "5d ago"
    assert format_time_ago(base - timedelta(days=365), now_dt=base) == "365d ago"


@pytest.mark.asyncio
async def test_dispatch_pending_notifications_burst_and_rate_limiting():
    from rss import dispatch_pending_notifications

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")

        # Enqueue 8 notifications for User 1
        for i in range(8):
            sm.enqueue_notification(
                user_id=101,
                title="Test Channel",
                url=f"https://youtube.com/watch?v=vid_{i}",
                video_id=f"vid_{i}",
                published="2026-09-13T10:00:00+00:00"
            )

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text, reply_markup))

        user_send_times = {}

        # Max 10 per minute: all 8 should be sent in one burst immediately
        sent_count = await dispatch_pending_notifications(
            state=sm,
            send_message_fn=mock_send,
            max_posts_per_min=10,
            user_send_times=user_send_times
        )

        assert sent_count == 8
        assert len(sent_messages) == 8
        assert sm.get_pending_notifications_count(101) == 0
        assert len(user_send_times[101]) == 8

        # Now enqueue 5 more posts (total 13 posts in rolling window)
        for i in range(8, 13):
            sm.enqueue_notification(
                user_id=101,
                title="Test Channel",
                url=f"https://youtube.com/watch?v=vid_{i}",
                video_id=f"vid_{i}",
                published="2026-09-13T10:00:00+00:00"
            )

        # Quota remaining is 10 - 8 = 2 posts!
        sent_count2 = await dispatch_pending_notifications(
            state=sm,
            send_message_fn=mock_send,
            max_posts_per_min=10,
            user_send_times=user_send_times
        )

        assert sent_count2 == 2
        assert len(sent_messages) == 10
        # 3 items must still be pending in queue
        assert sm.get_pending_notifications_count(101) == 3


@pytest.mark.asyncio
async def test_dispatch_pending_notifications_user_isolation_and_error():
    from rss import dispatch_pending_notifications

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")

        # User 1 has 15 posts (exceeding limit of 10)
        for i in range(15):
            sm.enqueue_notification(
                user_id=101,
                title="Channel A",
                url=f"https://youtube.com/watch?v=u1_vid_{i}",
                video_id=f"u1_vid_{i}"
            )

        # User 2 has 3 posts
        for i in range(3):
            sm.enqueue_notification(
                user_id=202,
                title="Channel B",
                url=f"https://youtube.com/watch?v=u2_vid_{i}",
                video_id=f"u2_vid_{i}"
            )

        # User 3 is blocked / forbidden
        sm.enqueue_notification(
            user_id=303,
            title="Channel C",
            url="https://youtube.com/watch?v=u3_vid_0",
            video_id="u3_vid_0"
        )

        sent_by_user = {101: [], 202: [], 303: []}

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            if chat_id == 303:
                raise Exception("Forbidden: bot was blocked by the user")
            sent_by_user[chat_id].append(text)

        user_send_times = {}

        sent_total = await dispatch_pending_notifications(
            state=sm,
            send_message_fn=mock_send,
            max_posts_per_min=10,
            user_send_times=user_send_times
        )

        # User 1 sent 10, User 2 sent 3 (total 13)
        assert sent_total == 13
        assert len(sent_by_user[101]) == 10
        assert len(sent_by_user[202]) == 3
        assert sm.get_pending_notifications_count(101) == 5
        assert sm.get_pending_notifications_count(202) == 0

        # User 3 was blocked, so notification was dropped from queue
        assert sm.get_pending_notifications_count(303) == 0


@pytest.mark.asyncio
async def test_all_video_messages_always_include_relative_timestamp():
    from rss import dispatch_pending_notifications
    from datetime import datetime, timezone, timedelta

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(f"{tmpdir}/state.json")

        now = datetime.now(timezone.utc)

        # 1. Fresh video (10s ago)
        sm.enqueue_notification(
            user_id=101,
            title="Channel A",
            url="https://youtube.com/watch?v=fresh",
            video_id="fresh",
            published=(now - timedelta(seconds=10)).isoformat()
        )

        # 2. Older video (3 hours ago)
        sm.enqueue_notification(
            user_id=101,
            title="Channel B",
            url="https://youtube.com/watch?v=older",
            video_id="older",
            published=(now - timedelta(hours=3)).isoformat()
        )

        # 3. Notification with no published date provided (fallback to now)
        sm.enqueue_notification(
            user_id=101,
            title="Channel C",
            url="https://youtube.com/watch?v=nopub",
            video_id="nopub",
            published=None
        )

        # 4. Notification with empty title
        sm.enqueue_notification(
            user_id=101,
            title="",
            url="https://youtube.com/watch?v=notitle",
            video_id="notitle",
            published=(now - timedelta(minutes=15)).isoformat()
        )

        # 5. Notification with HTML special characters in channel title
        sm.enqueue_notification(
            user_id=101,
            title="Rock & Roll <Live>",
            url="https://youtube.com/watch?v=escaped",
            video_id="escaped",
            published=(now - timedelta(seconds=30)).isoformat()
        )

        sent_messages = []

        async def mock_send(chat_id: int, text: str, reply_markup=None):
            sent_messages.append((chat_id, text))

        await dispatch_pending_notifications(
            state=sm,
            send_message_fn=mock_send,
            max_posts_per_min=0
        )

        assert len(sent_messages) == 5

        # Every single message must end with a relative timestamp in parentheses
        for chat_id, text in sent_messages:
            assert re.search(r"\(\d+(?:s|m|h|d) ago\)$", text), f"Message does not end with relative timestamp: {text}"

        assert "<b>Channel A</b> https://youtube.com/watch?v=fresh" in sent_messages[0][1]
        assert "<b>Channel B</b> https://youtube.com/watch?v=older (3h ago)" in sent_messages[1][1]
        assert "<b>Channel C</b> https://youtube.com/watch?v=nopub" in sent_messages[2][1]
        assert sent_messages[3][1].startswith("https://youtube.com/watch?v=notitle (15m ago)")
        assert "<b>Rock &amp; Roll &lt;Live&gt;</b> https://youtube.com/watch?v=escaped (30s ago)" == sent_messages[4][1]



