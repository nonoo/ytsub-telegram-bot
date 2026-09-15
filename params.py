import argparse
import json
import logging
import os
import sys
from typing import List

logger = logging.getLogger(__name__)


class Params:
    def __init__(self):
        self.bot_token: str = ""
        self.allowed_user_ids: List[int] = []
        self.admin_user_ids: List[int] = []
        self.state_file: str = "ytsub-state.json"
        self.check_interval_sec: int = 300
        self.subscription_sync_interval_sec: int = 43200
        self.max_posts_per_min: int = 10
        self.google_client_id: str = ""
        self.google_client_secret: str = ""
        self.google_client_secret_file: str = ""

    @property
    def check_interval(self) -> int:
        return self.check_interval_sec

    def parse(self, args: List[str] = None) -> None:
        parser = argparse.ArgumentParser(description="YTSub Telegram Bot")
        parser.add_argument("--bot-token", default="", help="Telegram bot token")
        parser.add_argument("--allowed-user-ids", default="", help="Comma-separated allowed Telegram user IDs")
        parser.add_argument("--admin-user-ids", default="", help="Comma-separated admin Telegram user IDs")
        parser.add_argument("--state-file", default="", help="Path to state JSON file")
        parser.add_argument("--check-interval-sec", "--check-interval", dest="check_interval_sec", type=int, default=0, help="Channel RSS check interval in seconds")
        parser.add_argument("--subscription-sync-interval-sec", dest="subscription_sync_interval_sec", type=int, default=0, help="YouTube subscription sync interval in seconds (default: 43200 / 12 hours)")
        parser.add_argument("--max-posts-per-min", type=int, default=None, help="Max video post notifications sent per minute per user (default: 10, set to 0 to disable)")
        parser.add_argument("--google-client-id", default="", help="Google OAuth Client ID")
        parser.add_argument("--google-client-secret", default="", help="Google OAuth Client Secret")
        parser.add_argument("--google-client-secret-file", default="", help="Path to client_secret.json file")

        parsed = parser.parse_args(args if args is not None else sys.argv[1:])

        self.bot_token = parsed.bot_token or os.getenv("BOT_TOKEN", "")
        if not self.bot_token:
            raise ValueError("bot token not set (provide --bot-token or BOT_TOKEN)")

        state_file_val = parsed.state_file or os.getenv("STATE_FILE", "")
        if state_file_val:
            self.state_file = state_file_val

        # Google OAuth client credentials (server-level)
        self.google_client_id = parsed.google_client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = parsed.google_client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.google_client_secret_file = parsed.google_client_secret_file or os.getenv("GOOGLE_CLIENT_SECRET_FILE", "")

        # Auto-detect client_secret.json in current directory if not specified
        if not self.google_client_secret_file and os.path.exists("client_secret.json"):
            self.google_client_secret_file = "client_secret.json"

        if self.google_client_secret_file and os.path.exists(self.google_client_secret_file):
            try:
                with open(self.google_client_secret_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    creds = data.get("installed") or data.get("web") or data
                    if not self.google_client_id and "client_id" in creds:
                        self.google_client_id = creds["client_id"]
                    if not self.google_client_secret and "client_secret" in creds:
                        self.google_client_secret = creds["client_secret"]
            except Exception as e:
                logger.warning("Failed to load client secret file %s: %s", self.google_client_secret_file, e)

        check_interval_val = parsed.check_interval_sec or os.getenv("CHECK_INTERVAL_SEC", os.getenv("CHECK_INTERVAL", ""))
        if check_interval_val:
            try:
                self.check_interval_sec = int(check_interval_val)
            except ValueError:
                raise ValueError(f"invalid check interval: {check_interval_val}")

        sub_sync_val = parsed.subscription_sync_interval_sec or os.getenv("SUBSCRIPTION_SYNC_INTERVAL_SEC", "")
        if sub_sync_val:
            try:
                val = int(sub_sync_val)
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid subscription sync interval: {sub_sync_val}") from e
            if val < 0:
                raise ValueError(f"--subscription-sync-interval-sec must be >= 0: {val}")
            self.subscription_sync_interval_sec = val

        max_posts_val = parsed.max_posts_per_min if parsed.max_posts_per_min is not None else os.getenv("MAX_POSTS_PER_MIN", "")
        if max_posts_val != "" and max_posts_val is not None:
            try:
                val = int(max_posts_val)
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid max posts per min: {max_posts_val}") from e
            if val < 0:
                raise ValueError(f"--max-posts-per-min must be >= 0: {val}")
            self.max_posts_per_min = val

        allowed_raw = parsed.allowed_user_ids or os.getenv("ALLOWED_USERIDS", "")
        if allowed_raw:
            for item in allowed_raw.split(","):
                item = item.strip()
                if item:
                    try:
                        self.allowed_user_ids.append(int(item))
                    except ValueError:
                        raise ValueError(f"invalid user ID in allowed-user-ids: {item}")

        admin_raw = parsed.admin_user_ids or os.getenv("ADMIN_USERIDS", "")
        if admin_raw:
            for item in admin_raw.split(","):
                item = item.strip()
                if item:
                    try:
                        uid = int(item)
                        self.admin_user_ids.append(uid)
                        if uid not in self.allowed_user_ids:
                            self.allowed_user_ids.append(uid)
                    except ValueError:
                        raise ValueError(f"invalid user ID in admin-user-ids: {item}")

    def is_user_allowed(self, user_id: int) -> bool:
        if not self.allowed_user_ids:
            return True
        return user_id in self.allowed_user_ids

    def is_user_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids
