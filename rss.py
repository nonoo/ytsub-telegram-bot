import asyncio
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import aiohttp
from dateutil import parser as date_parser
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from state import StateManager, parse_human_datetime, parse_human_timestamp, utc_now

logger = logging.getLogger(__name__)

ERROR_ALERT_THRESHOLD = 10


class FeedFetchError(Exception):
    """Raised when an RSS feed cannot be fetched via HTTP."""
    pass


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


def format_time_ago(published_dt: datetime, now_dt: Optional[datetime] = None) -> str:
    """
    Formats the elapsed time since published_dt in human format:
    - Under 1 minute: '(Xs ago)' (e.g. '5s ago')
    - Under 60 minutes: '(Xm ago)' (e.g. '5m ago')
    - 60 minutes to 23 hours: '(Xh ago)' (e.g. '5h ago')
    - 24 hours or more: '(Xd ago)' (e.g. '5d ago')
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    if published_dt.tzinfo is None:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    elapsed = max(0, int((now_dt - published_dt).total_seconds()))

    if elapsed < 60:
        return f"{elapsed}s ago"
    elif elapsed < 3600:
        mins = elapsed // 60
        return f"{mins}m ago"
    elif elapsed < 86400:
        hours = elapsed // 3600
        return f"{hours}h ago"
    else:
        days = elapsed // 86400
        return f"{days}d ago"


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
    if not xml_text or not xml_text.strip():
        raise ValueError("Empty feed content")

    root = ET.fromstring(xml_text)
    root_tag_lower = root.tag.lower()
    if "feed" not in root_tag_lower and "rss" not in root_tag_lower:
        raise ValueError(f"Invalid feed format (root element: <{root.tag}>)")

    entries: List[VideoEntry] = []
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


async def fetch_feed_url(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.text()
            else:
                logger.warning("Failed to fetch RSS from %s: HTTP %s", url, resp.status)
                raise FeedFetchError(f"HTTP {resp.status}")
    except Exception as e:
        if isinstance(e, FeedFetchError):
            raise
        logger.warning("Network error fetching RSS from %s: %s", url, e)
        raise FeedFetchError(f"Network error: {e}")


async def fetch_channel_rss(session: aiohttp.ClientSession, channel_id: str) -> str:
    return await fetch_feed_url(session, RSS_BASE_URL.format(channel_id=channel_id))


async def check_channels_and_notify(
    state: StateManager,
    send_message_fn: Callable[..., Coroutine[Any, Any, None]],
    filter_user_id: Optional[int] = None,
    min_seconds_since_check: float = 0.0,
    auto_dispatch: bool = False,
    max_posts_per_min: int = 10,
    user_send_times: Optional[Dict[int, List[float]]] = None
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
    user_errors_to_alert: Dict[int, List[str]] = defaultdict(list)
    user_recoveries_to_alert: Dict[int, List[str]] = defaultdict(list)
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
                error_msg: Optional[str] = None
                feed_author = ""
                entries: List[VideoEntry] = []

                if isinstance(feed_result, Exception):
                    error_msg = str(feed_result)
                elif not feed_result:
                    error_msg = "Empty response from server"
                else:
                    try:
                        feed_author, entries = parse_feed(feed_result)
                    except Exception as e:
                        error_msg = str(e)

                if error_msg:
                    logger.warning("Feed error for %s: %s", url, error_msg)
                    for user_id, is_custom, key, last_pub_val, ch_title in subscribers:
                        display_title = ch_title or key
                        should_alert = state.record_feed_error(
                            user_id=user_id,
                            is_custom=is_custom,
                            key=key,
                            error_msg=error_msg,
                            threshold=ERROR_ALERT_THRESHOLD
                        )
                        if should_alert:
                            if display_title not in user_errors_to_alert[user_id]:
                                user_errors_to_alert[user_id].append(display_title)

                        if is_custom:
                            state.update_custom_feed_timestamps(
                                user_id=user_id,
                                url=key,
                                last_checked_epoch=check_timestamp_epoch
                            )
                        else:
                            state.update_channel_timestamps(
                                user_id=user_id,
                                channel_id=key,
                                last_checked_epoch=check_timestamp_epoch
                            )
                    continue

                # Successful fetch and parse
                checked_count += 1

                for user_id, is_custom, key, last_pub_val, ch_title in subscribers:
                    display_title = ch_title or feed_author or "Channel"

                    was_failing = state.reset_feed_error(
                        user_id=user_id,
                        is_custom=is_custom,
                        key=key,
                        threshold=ERROR_ALERT_THRESHOLD
                    )
                    if was_failing:
                        if display_title not in user_recoveries_to_alert[user_id]:
                            user_recoveries_to_alert[user_id].append(display_title)

                    last_pub_dt = parse_human_datetime(last_pub_val)
                    newest_pub_dt = None

                    for entry in entries:
                        if last_pub_dt is None or entry.published_dt > last_pub_dt:
                            # New video! Log update and enqueue for gradual delivery
                            logger.info(
                                "RSS update found for '%s' (%s): '%s' (%s) -> enqueuing notification for user %s",
                                display_title,
                                key,
                                entry.title,
                                entry.url,
                                user_id
                            )
                            state.enqueue_notification(
                                user_id=user_id,
                                title=display_title,
                                url=entry.url,
                                video_id=entry.video_id,
                                published=entry.published_iso or entry.published_dt.isoformat()
                            )

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

    if send_message_fn is not None:
        for user_id, err_feeds in user_errors_to_alert.items():
            if not err_feeds:
                continue
            feed_str = ", ".join(err_feeds[:10])
            if len(err_feeds) > 10:
                feed_str += ", ... and more"
            msg = f"Error updating: {feed_str}"
            try:
                await send_message_fn(user_id, msg)
            except Exception as e:
                logger.error("Failed to send error notification to %s: %s", user_id, e)

        for user_id, rec_feeds in user_recoveries_to_alert.items():
            if not rec_feeds:
                continue
            feed_str = ", ".join(rec_feeds[:10])
            if len(rec_feeds) > 10:
                feed_str += ", ... and more"
            msg = f"Working again: {feed_str}"
            try:
                await send_message_fn(user_id, msg)
            except Exception as e:
                logger.error("Failed to send recovery notification to %s: %s", user_id, e)

    if auto_dispatch and send_message_fn is not None:
        await dispatch_pending_notifications(
            state=state,
            send_message_fn=send_message_fn,
            max_posts_per_min=max_posts_per_min,
            user_send_times=user_send_times
        )

    return checked_count


async def dispatch_pending_notifications(
    state: StateManager,
    send_message_fn: Callable[..., Coroutine[Any, Any, None]],
    max_posts_per_min: int = 10,
    user_send_times: Optional[Dict[int, List[float]]] = None
) -> int:
    """
    Gradually dispatches pending notifications to users according to max_posts_per_min (Option A).
    Allows bursts up to max_posts_per_min within any rolling 60-second window per user.
    Calculates dynamic relative timestamp (e.g. '(5m ago)') at send time.
    Returns total number of messages sent in this call.
    """
    if user_send_times is None:
        user_send_times = {}

    sent_count = 0
    now = time.monotonic()
    now_utc = utc_now()

    for user_id in state.get_users_with_pending_notifications():
        if user_id not in user_send_times:
            user_send_times[user_id] = []

        # Purge send timestamps older than 60 seconds
        user_send_times[user_id] = [t for t in user_send_times[user_id] if (now - t) < 60.0]

        quota = (max_posts_per_min - len(user_send_times[user_id])) if max_posts_per_min > 0 else 999999

        while quota > 0:
            notification = state.peek_pending_notification(user_id)
            if not notification:
                break

            title = notification.get("title", "")
            url = notification.get("url", "")
            video_id = notification.get("video_id")
            pub_raw = notification.get("published")

            pub_dt = parse_human_datetime(pub_raw) if pub_raw else None
            if not pub_dt and pub_raw:
                try:
                    pub_dt = date_parser.parse(pub_raw)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = None

            if not pub_dt:
                pub_dt = now_utc

            time_ago = format_time_ago(pub_dt, now_dt=now_utc)
            if title:
                escaped_title = html.escape(title)
                message = f"<b>{escaped_title}</b> {url} ({time_ago})"
            else:
                message = f"{url} ({time_ago})"

            reply_markup = None
            if video_id:
                keyboard = [
                    [
                        InlineKeyboardButton("🕒 Watch Later", callback_data=f"wl:{video_id}"),
                        InlineKeyboardButton("🎧 Listen Later", callback_data=f"ll:{video_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                if reply_markup:
                    try:
                        await send_message_fn(user_id, message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
                    except TypeError:
                        try:
                            await send_message_fn(user_id, message, reply_markup=reply_markup)
                        except TypeError:
                            await send_message_fn(user_id, message)
                else:
                    try:
                        await send_message_fn(user_id, message, parse_mode=ParseMode.HTML)
                    except TypeError:
                        await send_message_fn(user_id, message)

                state.pop_pending_notification(user_id)
                send_time = time.monotonic()
                user_send_times[user_id].append(send_time)
                now = send_time
                sent_count += 1
                quota -= 1

                # Brief sleep between burst messages to respect per-second flood limits
                if quota > 0 and state.peek_pending_notification(user_id):
                    await asyncio.sleep(0.2)
            except Exception as e:
                err_str = str(e).lower()
                if "forbidden" in err_str or "blocked" in err_str or "chat not found" in err_str or "user is deactivated" in err_str:
                    logger.warning("User %s is unreachable (%s). Dropping pending notification.", user_id, e)
                    state.pop_pending_notification(user_id)
                    quota -= 1
                else:
                    logger.error("Failed to send pending notification to %s: %s", user_id, e)
                    # Temporary failure: pause sending to this user for this tick
                    break

    return sent_count
