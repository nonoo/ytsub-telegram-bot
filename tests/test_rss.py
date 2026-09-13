import asyncio
import logging
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
                send_message_fn=mock_send
            )

            assert checked > 0
            # User 1 should get notified about vid222, but NOT vid111
            assert len(sent_messages) == 1
            chat_id, text, reply_markup = sent_messages[0]
            assert chat_id == 101
            assert text == "[Channel X] https://www.youtube.com/watch?v=vid222"
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
                send_message_fn=mock_send
            )

            assert checked == 1
            assert len(sent_messages) == 1
            chat_id, text, reply_markup = sent_messages[0]
            assert chat_id == 101
            assert text == "[Custom Author] https://www.youtube.com/watch?v=cust1"
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
            assert '⚠️ Error updating feed "Broken Channel" (10 consecutive failures):' in text
            assert "HTTP 500" in text

            ch = sm.get_user(101)["channels"]["UC_ERR"]
            assert ch["error_count"] == 10

            # 11th failure: error_count stays 10, no new alert sent
            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 0
            assert len(sent_messages) == 1
            assert sm.get_user(101)["channels"]["UC_ERR"]["error_count"] == 10

            # 12th run: feed recovers!
            mock_fetch.side_effect = None
            mock_fetch.return_value = SAMPLE_FEED

            checked = await check_channels_and_notify(state=sm, send_message_fn=mock_send)
            assert checked == 1
            # A recovery notification should have been sent
            assert len(sent_messages) == 2
            chat_id, text = sent_messages[1]
            assert chat_id == 101
            assert '✅ Feed "Broken Channel" is working again.' in text

            # State error_count should be reset to 0 and last_error to None
            ch = sm.get_user(101)["channels"]["UC_ERR"]
            assert ch["error_count"] == 0
            assert ch["last_error"] is None


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

