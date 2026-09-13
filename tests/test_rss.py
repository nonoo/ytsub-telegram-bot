import asyncio
import logging
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest
from rss import VideoEntry, check_channels_and_notify, parse_atom_feed
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

        async def mock_send(chat_id: int, text: str):
            sent_messages.append((chat_id, text))

        with patch("rss.fetch_channel_rss", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = SAMPLE_FEED

            checked = await check_channels_and_notify(
                state=sm,
                send_message_fn=mock_send
            )

            assert checked > 0
            # User 1 should get notified about vid222, but NOT vid111
            assert len(sent_messages) == 1
            chat_id, text = sent_messages[0]
            assert chat_id == 101
            assert text == "Channel X https://www.youtube.com/watch?v=vid222"

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
