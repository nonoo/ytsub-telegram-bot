import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

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


class StateManager:
    def __init__(self, file_path: str = "ytsub-state.json"):
        self.file_path = file_path
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

            # Clean up / migrate: Ensure only human format is used for last_checked and last_published.
            # Also remove legacy client_id, client_secret, and chat_id if present.
            migrated = False
            for user in self.data.get("users", {}).values():
                if "client_id" in user:
                    user.pop("client_id", None)
                    migrated = True
                if "client_secret" in user:
                    user.pop("client_secret", None)
                    migrated = True
                if "chat_id" in user:
                    user.pop("chat_id", None)
                    migrated = True

                for ch in user.get("channels", {}).values():
                    # last_checked
                    if "last_checked_human" in ch:
                        ch["last_checked"] = ch.pop("last_checked_human")
                        migrated = True
                    elif isinstance(ch.get("last_checked"), (int, float)):
                        ch["last_checked"] = format_human_timestamp(epoch=ch["last_checked"])
                        migrated = True

                    # last_published
                    if "last_published_human" in ch:
                        ch["last_published"] = ch.pop("last_published_human")
                        migrated = True
                    elif "last_published" in ch:
                        lp_val = ch["last_published"]
                        if isinstance(lp_val, str) and not lp_val.endswith(" UTC"):
                            dt = parse_human_datetime(lp_val)
                            if dt:
                                ch["last_published"] = format_human_timestamp(dt=dt)
                                migrated = True
                        elif not isinstance(lp_val, str):
                            dt = parse_human_datetime(lp_val)
                            if dt:
                                ch["last_published"] = format_human_timestamp(dt=dt)
                                migrated = True
            if migrated:
                self.save()

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
                "channels": {}
            }
        return self.data["users"][uid]

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
                updated_channels[ch_id] = ch_data
            else:
                new_count += 1
                updated_channels[ch_id] = {
                    "title": title,
                    "last_published": now_human,
                    "last_checked": now_human
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
