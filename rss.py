import asyncio
import logging
import re
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
YOUTUBE_VIDEO_ID_REGEX = re.compile(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})")


def normalize_feed_url(input_str: str) -> str:
    s = input_str.strip()
    if not s:
        return ""
    # Direct YouTube channel ID (e.g. UC_x5XG1OV2P6uZZ5FSM9Ttw)
    if re.match(r"^UC[a-zA-Z0-9_-]{20,}$", s):
        return RSS_BASE_URL.format(channel_id=s)
    # YouTube channel URL: https://www.youtube.com/channel/UC...
    m_ch = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]+)", s)
    if m_ch:
        return RSS_BASE_URL.format(channel_id=m_ch.group(1))
    # YouTube playlist URL: https://www.youtube.com/playlist?list=PL...
    m_pl = re.search(r"youtube\.com/playlist\?.*list=([a-zA-Z0-9_-]+)", s)
    if m_pl:
        return f"https://www.youtube.com/feeds/videos.xml?playlist_id={m_pl.group(1)}"
    return s


class VideoEntry:
    def __init__(
        self,
        video_id: Optional[str],
        title: str,
        published_dt: datetime,
        published_iso: str,
        url: Optional[str] = None
    ):
        self.video_id = video_id
        self.title = title
        self.published_dt = published_dt
        self.published_iso = published_iso
        self._url = url

    @property
    def url(self) -> str:
        if self._url:
            return self._url
        if self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return ""


def extract_feed_author(root: ET.Element) -> str:
    """Extracts feed/channel author name from XML, falling back to title."""
    # 1. Feed-level <author><name>
    for path in ("atom:author/atom:name", "author/name", "atom:author", "author"):
        el = root.find(path, ATOM_NS) if "atom:" in path else root.find(path)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()

    # 2. Entry-level <author><name> from first entry
    for entry_tag in ("atom:entry", "entry", "item"):
        entry_el = root.find(entry_tag, ATOM_NS) if "atom:" in entry_tag else root.find(entry_tag)
        if entry_el is not None:
            for path in ("atom:author/atom:name", "author/name", "atom:author", "author"):
                el = entry_el.find(path, ATOM_NS) if "atom:" in path else entry_el.find(path)
                if el is not None and el.text and el.text.strip():
                    return el.text.strip()
            break

    # 3. Fallback to <title>
    for title_path in ("atom:title", "title", "channel/title"):
        el = root.find(title_path, ATOM_NS) if "atom:" in title_path else root.find(title_path)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()

    return ""


def parse_feed(xml_text: str) -> Tuple[str, List[VideoEntry]]:
    """Parses feed XML (Atom or RSS), returning (author_name, entries)."""
    entries: List[VideoEntry] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse RSS XML: %s", e)
        return "", entries

    feed_author = extract_feed_author(root)

    # Find entries: check atom:entry, entry, item, channel/item
    raw_entries = root.findall("atom:entry", ATOM_NS)
    if not raw_entries:
        raw_entries = root.findall("entry")
    if not raw_entries:
        raw_entries = root.findall("item")
    if not raw_entries:
        raw_entries = root.findall("channel/item")

    for entry_el in raw_entries:
        # 1. Video ID
        video_id = None
        video_id_el = entry_el.find("yt:videoId", ATOM_NS)
        if video_id_el is not None and video_id_el.text:
            video_id = video_id_el.text.strip()

        # 2. Link / URL
        entry_url = None
        link_el = entry_el.find("atom:link", ATOM_NS)
        if link_el is None:
            link_el = entry_el.find("link")
        if link_el is not None:
            href = link_el.attrib.get("href") or link_el.text or ""
            entry_url = href.strip()
            if not video_id and entry_url:
                m = YOUTUBE_VIDEO_ID_REGEX.search(entry_url)
                if m:
                    video_id = m.group(1)

        if not video_id and not entry_url:
            continue

        # 3. Title
        title_el = entry_el.find("atom:title", ATOM_NS)
        if title_el is None:
            title_el = entry_el.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        # 4. Published date
        published_el = entry_el.find("atom:published", ATOM_NS)
        if published_el is None:
            published_el = entry_el.find("published")
        if published_el is None:
            published_el = entry_el.find("pubDate")
        if published_el is None:
            published_el = entry_el.find("atom:updated", ATOM_NS)
        if published_el is None:
            published_el = entry_el.find("updated")

        published_str = published_el.text.strip() if published_el is not None and published_el.text else ""
        try:
            pub_dt = date_parser.parse(published_str)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = utc_now()

        entries.append(
            VideoEntry(
                video_id=video_id,
                title=title,
                published_dt=pub_dt,
                published_iso=published_str,
                url=entry_url
            )
        )

    # Sort oldest to newest so notifications are dispatched in order of release
    entries.sort(key=lambda x: x.published_dt)
    return feed_author, entries


def parse_atom_feed(xml_text: str) -> List[VideoEntry]:
    return parse_feed(xml_text)[1]


async def fetch_feed_url(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.text()
            else:
                logger.warning("Failed to fetch RSS from %s: HTTP %s", url, resp.status)
                return None
    except Exception as e:
        logger.warning("Network error fetching RSS from %s: %s", url, e)
        return None


async def fetch_channel_rss(session: aiohttp.ClientSession, channel_id: str) -> Optional[str]:
    return await fetch_feed_url(session, RSS_BASE_URL.format(channel_id=channel_id))


async def check_channels_and_notify(
    state: StateManager,
    send_message_fn: Callable[[int, str], Coroutine[Any, Any, None]],
    filter_user_id: Optional[int] = None,
    min_seconds_since_check: float = 0.0
) -> int:
    """
    Checks subscribed channels and custom feeds and notifies users about new videos.
    Returns the count of feeds checked.
    """
    now_epoch = time.time()
    users_data = state.data.get("users", {})

    # Map feed_url -> list of (user_id, is_custom, key, last_published_val, title)
    feeds_to_check: Dict[str, List[Tuple[int, bool, str, str, str]]] = {}

    for uid_str, user_info in users_data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        if filter_user_id is not None and uid != filter_user_id:
            continue

        # 1. Standard YouTube channels (require active credentials)
        if user_info.get("token") or user_info.get("refresh_token"):
            channels = user_info.get("channels", {})
            for ch_id, ch_info in channels.items():
                last_checked = parse_human_timestamp(ch_info.get("last_checked"))
                if min_seconds_since_check > 0.0 and (now_epoch - last_checked) < min_seconds_since_check:
                    continue
                last_pub = ch_info.get("last_published", "")
                title = ch_info.get("title", "")
                url = RSS_BASE_URL.format(channel_id=ch_id)
                feeds_to_check.setdefault(url, []).append((uid, False, ch_id, last_pub, title))

        # 2. Custom feeds (independent of OAuth credentials)
        custom_feeds = user_info.get("custom_feeds", {})
        for feed_url, feed_info in custom_feeds.items():
            last_checked = parse_human_timestamp(feed_info.get("last_checked"))
            if min_seconds_since_check > 0.0 and (now_epoch - last_checked) < min_seconds_since_check:
                continue
            last_pub = feed_info.get("last_published", "")
            title = feed_info.get("title", "")
            feeds_to_check.setdefault(feed_url, []).append((uid, True, feed_url, last_pub, title))

    if not feeds_to_check:
        return 0

    checked_count = 0
    async with aiohttp.ClientSession() as session:
        # Fetch RSS feeds concurrently in chunks
        chunk_size = 10
        feed_items = list(feeds_to_check.items())

        for i in range(0, len(feed_items), chunk_size):
            chunk = feed_items[i:i + chunk_size]
            tasks = []
            for url, _ in chunk:
                m = re.match(r"^https://www\.youtube\.com/feeds/videos\.xml\?channel_id=([a-zA-Z0-9_-]+)$", url)
                if m:
                    tasks.append(fetch_channel_rss(session, m.group(1)))
                else:
                    tasks.append(fetch_feed_url(session, url))
            results = await asyncio.gather(*tasks, return_exceptions=True)

            check_timestamp_epoch = time.time()

            for (url, subscribers), feed_result in zip(chunk, results):
                if isinstance(feed_result, Exception) or not feed_result:
                    continue

                feed_author, entries = parse_feed(feed_result)
                checked_count += 1

                for user_id, is_custom, key, last_pub_val, ch_title in subscribers:
                    display_title = ch_title or feed_author or "Channel"
                    last_pub_dt = parse_human_datetime(last_pub_val)
                    newest_pub_dt = None

                    for entry in entries:
                        if last_pub_dt is None or entry.published_dt > last_pub_dt:
                            # New video! Log update and notify user
                            logger.info(
                                "RSS update found for '%s' (%s): '%s' (%s) -> notifying user %s",
                                display_title,
                                key,
                                entry.title,
                                entry.url,
                                user_id
                            )
                            message = f"[{display_title}] {entry.url}"

                            reply_markup = None
                            if entry.video_id:
                                keyboard = [
                                    [
                                        InlineKeyboardButton("🕒 Watch Later", callback_data=f"wl:{entry.video_id}"),
                                        InlineKeyboardButton("🎧 Listen Later", callback_data=f"ll:{entry.video_id}")
                                    ]
                                ]
                                reply_markup = InlineKeyboardMarkup(keyboard)

                            try:
                                if reply_markup:
                                    try:
                                        await send_message_fn(user_id, message, reply_markup=reply_markup)
                                    except TypeError:
                                        await send_message_fn(user_id, message)
                                else:
                                    await send_message_fn(user_id, message)
                            except Exception as e:
                                logger.error("Failed to send message to %s: %s", user_id, e)

                            newest_pub_dt = entry.published_dt
                            last_pub_dt = entry.published_dt

                    if is_custom:
                        state.update_custom_feed_timestamps(
                            user_id=user_id,
                            url=key,
                            last_published=newest_pub_dt,
                            last_checked_epoch=check_timestamp_epoch
                        )
                    else:
                        state.update_channel_timestamps(
                            user_id=user_id,
                            channel_id=key,
                            last_published=newest_pub_dt,
                            last_checked_epoch=check_timestamp_epoch
                        )

    return checked_count

