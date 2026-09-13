import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import aiohttp
from dateutil import parser as date_parser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from state import StateManager, parse_human_datetime, parse_human_timestamp, utc_now

logger = logging.getLogger(__name__)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
RSS_BASE_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


class VideoEntry:
    def __init__(self, video_id: str, title: str, published_dt: datetime, published_iso: str):
        self.video_id = video_id
        self.title = title
        self.published_dt = published_dt
        self.published_iso = published_iso

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def parse_atom_feed(xml_text: str) -> List[VideoEntry]:
    entries: List[VideoEntry] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse RSS XML: %s", e)
        return entries

    for entry_el in root.findall("atom:entry", ATOM_NS):
        video_id_el = entry_el.find("yt:videoId", ATOM_NS)
        title_el = entry_el.find("atom:title", ATOM_NS)
        published_el = entry_el.find("atom:published", ATOM_NS)

        if video_id_el is None or not video_id_el.text:
            continue

        video_id = video_id_el.text.strip()
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        published_str = published_el.text.strip() if published_el is not None and published_el.text else ""

        try:
            pub_dt = date_parser.isoparse(published_str)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = utc_now()

        entries.append(VideoEntry(video_id=video_id, title=title, published_dt=pub_dt, published_iso=published_str))

    # Sort oldest to newest so notifications are dispatched in order of release
    entries.sort(key=lambda x: x.published_dt)
    return entries


async def fetch_channel_rss(session: aiohttp.ClientSession, channel_id: str) -> Optional[str]:
    url = RSS_BASE_URL.format(channel_id=channel_id)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.text()
            else:
                logger.warning("Failed to fetch RSS for %s: HTTP %s", channel_id, resp.status)
                return None
    except Exception as e:
        logger.warning("Network error fetching RSS for %s: %s", channel_id, e)
        return None


async def check_channels_and_notify(
    state: StateManager,
    send_message_fn: Callable[[int, str], Coroutine[Any, Any, None]],
    filter_user_id: Optional[int] = None,
    min_seconds_since_check: float = 0.0
) -> int:
    """
    Checks subscribed channels and notifies users about new videos.
    Returns the count of channels checked.
    """
    now_epoch = time.time()
    users_data = state.data.get("users", {})

    # Determine which channels need checking
    # Map channel_id -> set of (user_id_int, last_published_iso, channel_title)
    channels_to_check: Dict[str, List[Tuple[int, str, str]]] = {}

    for uid_str, user_info in users_data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        if filter_user_id is not None and uid != filter_user_id:
            continue

        # Check if user has active credentials
        if not (user_info.get("token") or user_info.get("refresh_token")):
            continue

        channels = user_info.get("channels", {})

        for ch_id, ch_info in channels.items():
            last_checked = parse_human_timestamp(ch_info.get("last_checked"))
            if min_seconds_since_check > 0.0 and (now_epoch - last_checked) < min_seconds_since_check:
                continue

            last_pub = ch_info.get("last_published", "")
            title = ch_info.get("title", "")
            channels_to_check.setdefault(ch_id, []).append((uid, last_pub, title))

    if not channels_to_check:
        return 0

    checked_count = 0
    async with aiohttp.ClientSession() as session:
        # Fetch RSS feeds concurrently in chunks
        chunk_size = 10
        ch_items = list(channels_to_check.items())

        for i in range(0, len(ch_items), chunk_size):
            chunk = ch_items[i:i + chunk_size]
            tasks = [fetch_channel_rss(session, ch_id) for ch_id, _ in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            check_timestamp_epoch = time.time()

            for (ch_id, subscribers), feed_result in zip(chunk, results):
                if isinstance(feed_result, Exception) or not feed_result:
                    continue

                entries = parse_atom_feed(feed_result)
                checked_count += 1

                for user_id, last_pub_val, ch_title in subscribers:
                    last_pub_dt = parse_human_datetime(last_pub_val)
                    newest_pub_dt = None

                    for entry in entries:
                        if last_pub_dt is None or entry.published_dt > last_pub_dt:
                            # New video! Log update and notify user
                            logger.info(
                                "RSS update found for '%s' (%s): '%s' (%s) -> notifying user %s",
                                ch_title,
                                ch_id,
                                entry.title,
                                entry.url,
                                user_id
                            )
                            message = f"[{ch_title}] {entry.url}"
                            keyboard = [
                                [
                                    InlineKeyboardButton("🕒 Watch Later", callback_data=f"wl:{entry.video_id}"),
                                    InlineKeyboardButton("🎧 Listen Later", callback_data=f"ll:{entry.video_id}")
                                ]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            try:
                                try:
                                    await send_message_fn(user_id, message, reply_markup=reply_markup)
                                except TypeError:
                                    await send_message_fn(user_id, message)
                            except Exception as e:
                                logger.error("Failed to send message to %s: %s", user_id, e)

                            newest_pub_dt = entry.published_dt
                            last_pub_dt = entry.published_dt

                    state.update_channel_timestamps(
                        user_id=user_id,
                        channel_id=ch_id,
                        last_published=newest_pub_dt,
                        last_checked_epoch=check_timestamp_epoch
                    )

    return checked_count
