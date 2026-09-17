import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_human_timestamp(dt: Optional[datetime] = None, epoch: Optional[float] = None) -> str:
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    if epoch is not None:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_human_datetime(val: Any) -> Optional[datetime]:
    """Parses a timestamp (human format 'YYYY-MM-DD HH:MM:SS UTC', ISO format, or epoch float) to timezone-aware UTC datetime."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if not isinstance(val, str):
        return None
    val_str = val.strip()
    if val_str.endswith(" UTC"):
        try:
            return datetime.strptime(val_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(val_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = date_parser.parse(val_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_human_timestamp(val: Any) -> float:
    """Parses a timestamp to epoch float."""
    dt = parse_human_datetime(val)
    return dt.timestamp() if dt is not None else 0.0


INT64_MAX = (1 << 63) - 1


class StateManager:
    def __init__(self, file_path: str = "ytsub-state.json"):
        self.file_path = os.path.abspath(file_path)
        self.data: Dict[str, Any] = {"users": {}}

    def load(self) -> None:
        if not os.path.exists(self.file_path):
            self.data = {"users": {}}
            logger.info("State file %s does not exist. Initialized with empty state.", self.file_path)
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    if "users" not in loaded or not isinstance(loaded["users"], dict):
                        loaded["users"] = {}
                    self.data = loaded
                else:
                    self.data = {"users": {}}

            # Ensure users dict is present
            logger.info("Loaded state from %s (tracked users: %d)", self.file_path, len(self.data["users"]))
        except Exception as e:
            logger.error("Failed to load state file %s: %s", self.file_path, e)
            self.data = {"users": {}}

    def reload(self) -> None:
        self.load()

    def save(self) -> None:
        dir_name = os.path.dirname(os.path.abspath(self.file_path)) or "."
        os.makedirs(dir_name, exist_ok=True)

        temp_file = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                temp_file = tf.name
                json.dump(self.data, tf, indent=2, ensure_ascii=False)
            os.replace(temp_file, self.file_path)
            logger.debug("Successfully saved state to %s", self.file_path)
        except Exception as e:
            logger.error("Error saving state to %s: %s", self.file_path, e)
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            raise

    def get_user(self, user_id: int) -> Dict[str, Any]:
        uid = str(user_id)
        if uid not in self.data["users"]:
            self.data["users"][uid] = {
                "token": "",
                "refresh_token": "",
                "channels": {},
                "custom_feeds": {},
                "pending_notifications": []
            }
        user = self.data["users"][uid]
        if "custom_feeds" not in user:
            user["custom_feeds"] = {}
        if "pending_notifications" not in user:
            user["pending_notifications"] = []
        return user

    def has_user_oauth_credentials(self, user_id: int) -> bool:
        uid = str(user_id)
        user = self.data["users"].get(uid)
        if not user:
            return False
        return bool(user.get("token") or user.get("refresh_token"))

    def set_user_tokens(self, user_id: int, token: str, refresh_token: str) -> None:
        user = self.get_user(user_id)
        if token:
            user["token"] = token
        if refresh_token:
            user["refresh_token"] = refresh_token
        self.save()

    def sync_user_channels(self, user_id: int, fetched_channels: Dict[str, str]) -> int:
        """
        fetched_channels: {channel_id: channel_title}
        Returns the count of newly added channels.
        """
        user = self.get_user(user_id)
        existing_channels = user.setdefault("channels", {})
        now_dt = utc_now()
        now_iso = now_dt.isoformat()
        now_human = format_human_timestamp(dt=now_dt)
        now_epoch = now_dt.timestamp()

        new_count = 0
        updated_channels: Dict[str, Dict[str, Any]] = {}

        for ch_id, title in fetched_channels.items():
            if ch_id in existing_channels:
                ch_data = dict(existing_channels[ch_id])
                ch_data["title"] = title
                ch_data.setdefault("error_count", 0)
                ch_data.setdefault("last_error", None)
                updated_channels[ch_id] = ch_data
            else:
                new_count += 1
                updated_channels[ch_id] = {
                    "title": title,
                    "last_published": now_human,
                    "last_checked": now_human,
                    "error_count": 0,
                    "last_error": None
                }

        user["channels"] = updated_channels
        self.save()
        return new_count

    def update_channel_timestamps(
        self,
        user_id: int,
        channel_id: str,
        last_published: Optional[Union[str, datetime]] = None,
        last_checked_epoch: Optional[float] = None,
        last_checked: Optional[str] = None,
        last_published_iso: Optional[str] = None,
    ) -> None:
        user = self.get_user(user_id)
        channels = user.get("channels", {})
        if channel_id not in channels:
            return

        ch = channels[channel_id]
        pub_val = last_published or last_published_iso
        if pub_val is not None:
            if isinstance(pub_val, datetime):
                ch["last_published"] = format_human_timestamp(dt=pub_val)
            elif isinstance(pub_val, str) and pub_val.endswith(" UTC"):
                ch["last_published"] = pub_val
            else:
                dt = parse_human_datetime(pub_val)
                ch["last_published"] = format_human_timestamp(dt=dt) if dt else str(pub_val)

        if last_checked is not None:
            ch["last_checked"] = last_checked
        elif last_checked_epoch is not None:
            ch["last_checked"] = format_human_timestamp(epoch=last_checked_epoch)

        ch.pop("last_checked_human", None)
        ch.pop("last_published_human", None)

        self.save()

    def get_user_playlist(self, user_id: int, playlist_key: str) -> Optional[str]:
        user = self.get_user(user_id)
        return user.get("playlists", {}).get(playlist_key)

    def set_user_playlist(self, user_id: int, playlist_key: str, playlist_id: str) -> None:
        user = self.get_user(user_id)
        user.setdefault("playlists", {})[playlist_key] = playlist_id
        self.save()

    def clear_user_playlist(self, user_id: int, playlist_key: str) -> None:
        user = self.get_user(user_id)
        if "playlists" in user and playlist_key in user["playlists"]:
            del user["playlists"][playlist_key]
            self.save()

    def get_user_custom_feeds(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        user = self.get_user(user_id)
        return user.setdefault("custom_feeds", {})

    def add_custom_feed(self, user_id: int, url: str, title: str) -> bool:
        """
        Adds a custom feed for the user with initial timestamps set to now.
        Returns True if newly added, False if already existed.
        """
        user = self.get_user(user_id)
        feeds = user.setdefault("custom_feeds", {})
        now_human = format_human_timestamp()
        is_new = url not in feeds
        if is_new:
            feeds[url] = {
                "title": title,
                "last_published": now_human,
                "last_checked": now_human,
                "error_count": 0,
                "last_error": None
            }
        else:
            feeds[url]["title"] = title
        self.save()
        return is_new

    def remove_custom_feed(self, user_id: int, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Removes a custom feed by 1-based index or by exact URL.
        Returns the removed feed dict (including 'title') if found, or None.
        """
        user = self.get_user(user_id)
        feeds = user.get("custom_feeds", {})
        target_url = None

        ident_str = str(identifier).strip()
        if ident_str.isdigit():
            idx = int(ident_str)
            keys = list(feeds.keys())
            if 1 <= idx <= len(keys):
                target_url = keys[idx - 1]

        if not target_url:
            if ident_str in feeds:
                target_url = ident_str
            else:
                for u in feeds.keys():
                    if u.lower() == ident_str.lower():
                        target_url = u
                        break

        if target_url and target_url in feeds:
            removed = feeds.pop(target_url)
            self.save()
            return removed
        return None

    def update_custom_feed_timestamps(
        self,
        user_id: int,
        url: str,
        last_published: Optional[Union[str, datetime]] = None,
        last_checked_epoch: Optional[float] = None,
        last_checked: Optional[str] = None,
    ) -> None:
        user = self.get_user(user_id)
        feeds = user.get("custom_feeds", {})
        if url not in feeds:
            return

        feed = feeds[url]
        if last_published is not None:
            if isinstance(last_published, datetime):
                feed["last_published"] = format_human_timestamp(dt=last_published)
            elif isinstance(last_published, str) and last_published.endswith(" UTC"):
                feed["last_published"] = last_published
            else:
                dt = parse_human_datetime(last_published)
                feed["last_published"] = format_human_timestamp(dt=dt) if dt else str(last_published)

        if last_checked is not None:
            feed["last_checked"] = last_checked
        elif last_checked_epoch is not None:
            feed["last_checked"] = format_human_timestamp(epoch=last_checked_epoch)

        self.save()

    def record_feed_error(
        self,
        user_id: int,
        is_custom: bool,
        key: str,
        error_msg: str,
        threshold: int = 50
    ) -> bool:
        """
        Increments error_count (capped at INT64_MAX) and updates last_error.
        Returns True if error_count just reached threshold (transitioned from threshold-1 to threshold),
        prompting a notification to the user. Returns False otherwise.
        """
        user = self.get_user(user_id)
        container = user.get("custom_feeds" if is_custom else "channels", {})
        if key not in container:
            return False

        feed = container[key]
        prev_count = feed.get("error_count", 0)
        new_count = min(prev_count + 1, INT64_MAX)
        feed["error_count"] = new_count
        feed["last_error"] = str(error_msg)
        self.save()
        return bool(new_count == threshold and prev_count < threshold)

    def reset_feed_error(
        self,
        user_id: int,
        is_custom: bool,
        key: str,
        threshold: int = 50
    ) -> bool:
        """
        Resets error_count to 0 and clears last_error when a feed is processed successfully.
        Returns True if the feed had previously reached the error threshold, prompting a recovery notification.
        """
        user = self.get_user(user_id)
        container = user.get("custom_feeds" if is_custom else "channels", {})
        if key not in container:
            return False

        feed = container[key]
        prev_count = feed.get("error_count", 0)
        has_changes = False
        if prev_count > 0:
            feed["error_count"] = 0
            has_changes = True
        if feed.get("last_error") is not None:
            feed["last_error"] = None
            has_changes = True

        if has_changes:
            self.save()

        return bool(prev_count >= threshold)

    def enqueue_notification(
        self,
        user_id: int,
        title: str,
        url: str,
        video_id: Optional[str] = None,
        published: Optional[str] = None
    ) -> bool:
        """
        Enqueues a pending notification for a user.
        Deduplicates by URL so the same video is not enqueued twice.
        Returns True if enqueued, False if duplicate.
        """
        user = self.get_user(user_id)
        queue = user.setdefault("pending_notifications", [])
        for item in queue:
            if item.get("url") == url:
                return False

        queue.append({
            "title": title,
            "url": url,
            "video_id": video_id,
            "published": published
        })
        self.save()
        return True

    def get_pending_notifications(self, user_id: int) -> List[Dict[str, Any]]:
        """Returns a copy of the user's pending notifications."""
        user = self.get_user(user_id)
        return list(user.get("pending_notifications", []))

    def peek_pending_notification(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Returns the oldest pending notification without removing it."""
        user = self.get_user(user_id)
        queue = user.get("pending_notifications", [])
        return queue[0] if queue else None

    def pop_pending_notification(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Pops the oldest pending notification and persists state."""
        user = self.get_user(user_id)
        queue = user.setdefault("pending_notifications", [])
        if not queue:
            return None
        item = queue.pop(0)
        self.save()
        return item

    def clear_pending_notifications(self, user_id: int) -> int:
        """Clears all pending notifications for a user and returns count of cleared items."""
        user = self.get_user(user_id)
        queue = user.setdefault("pending_notifications", [])
        count = len(queue)
        if count > 0:
            user["pending_notifications"] = []
            self.save()
        return count

    def get_users_with_pending_notifications(self) -> List[int]:
        """Returns a list of user IDs (as ints) that have non-empty pending_notifications."""
        users_with_pending = []
        for uid_str, user_info in self.data.get("users", {}).items():
            if user_info.get("pending_notifications"):
                try:
                    users_with_pending.append(int(uid_str))
                except ValueError:
                    pass
        return users_with_pending

    def get_pending_notifications_count(self, user_id: Optional[int] = None) -> int:
        """Returns pending notifications count for a specific user, or total across all users."""
        if user_id is not None:
            user = self.get_user(user_id)
            return len(user.get("pending_notifications", []))
        total = 0
        for user_info in self.data.get("users", {}).values():
            total += len(user_info.get("pending_notifications", []))
        return total



