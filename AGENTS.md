# AGENTS.md: Developer & Agent Reference for YTSub Telegram Bot

This document outlines the codebase architecture, design patterns, state management, and conventions for AI agents and developers working on `ytsub-telegram-bot`.

---

## 1. Overview & System Purpose

`YTSub` is a multi-user Telegram bot written in Python that monitors YouTube channels for new video uploads using channel Atom RSS feeds and posts new video links directly to subscribed users.

### Key Characteristics
- **Multi-user Isolation**: Each allowed Telegram user connects their own YouTube account via OAuth 2.0 with targeted channel subscriptions.
- **Targeted Video Notifications**: Users only receive alerts for channels they subscribe to.
- **Initial Sync Quietness**: Newly subscribed channels and custom feeds record the current timestamp upon addition; existing/past videos are never posted.
- **Periodic & On-demand RSS Polling**: Periodic checks run every 5 minutes (`CHECK_INTERVAL_SEC`), with on-demand checks available via `/reload` (polling channels and feeds checked > 5 minutes ago).
- **Atomic State Persistence**: Persistent state is written atomically to `ytsub-state.json`.

---

## 2. Directory Structure & Key Files

```
ytsub-telegram-bot/
├── .gitignore                    # Ignores sensitive config, state, and .venv
├── .venv/                        # Host Python virtual environment (ignored in git)
├── AGENTS.md                     # Architecture reference for AI agents
├── Dockerfile                    # Container definition (no venv inside container)
├── LICENSE                       # MIT License
├── README.md                     # User-facing setup & usage documentation
├── logo.png                      # Project logo image
├── config.inc.sh-example         # Template for environment variables
├── params.py                     # Command-line & environment configuration parser
├── state.py                      # Multi-user state persistence & atomic json handling
├── youtube.py                    # Google OAuth flow & YouTube Data API v3 client
├── rss.py                        # Async Atom RSS feed polling & dispatching
├── handlers.py                   # Telegram command, callback, & message handlers
├── main.py                       # Application entry point & job queue scheduler
├── pytest.ini                    # Pytest configuration (pythonpath = .)
├── requirements.txt              # Production and testing Python dependencies
├── run.sh                        # Host launcher script (manages .venv)
├── 01-buildah-image.sh           # Container build script
├── 02-buildah-login.sh           # Registry login script
├── 03-buildah-push.sh            # Container push script
├── 03-clean.sh                   # Local container cleanup script
└── tests/
    ├── test_params.py            # CLI and environment parsing unit tests
    ├── test_state.py             # State isolation & persistence unit tests
    ├── test_rss.py               # RSS parsing & multi-user notification tests
    ├── test_handlers.py          # Command handlers and auth check tests
    └── test_youtube_playlists.py # YouTube playlist interaction unit tests
```

---

## 3. Module Responsibilities

### `params.py`
- Parses CLI arguments (`--bot-token`, `--admin-user-ids`, `--allowed-user-ids`, `--state-file`, `--check-interval-sec`, `--google-client-id`, `--google-client-secret`, `--google-client-secret-file`) and matching OS environment variables (`BOT_TOKEN`, `ADMIN_USERIDS`, `ALLOWED_USERIDS`, `STATE_FILE`, `CHECK_INTERVAL_SEC`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CLIENT_SECRET_FILE`). Auto-detects `client_secret.json` in the working directory if present.
- Manages user access lists (`is_user_allowed`, `is_user_admin`). Admin users are automatically included in allowed users.

### `state.py`
- Manages `ytsub-state.json` via the `StateManager` class.
- Uses atomic writes (`NamedTemporaryFile` + `os.replace`) to protect against corruption during crashes or restarts.
- Manages per-user records (`token`, `refresh_token`, `playlists`, `channels`, `custom_feeds`).
- Tracks channel and custom feed timestamps:
  - `last_published` in human format (`YYYY-MM-DD HH:MM:SS UTC`).
  - `last_checked` in human format (`YYYY-MM-DD HH:MM:SS UTC`).
  - `error_count` (int, default 0, capped at 10) and `last_error` (string or null).
- Handles subscription syncing: `sync_user_channels()` sets current timestamp for new channels so past videos are not alerted, and preserves `custom_feeds`.
- Manages custom RSS feeds: `get_user_custom_feeds()`, `add_custom_feed()`, `remove_custom_feed()`, `update_custom_feed_timestamps()`.
- Tracks errors and recoveries: `record_feed_error()` and `reset_feed_error()`.

### `youtube.py`
- Encapsulates Google OAuth 2.0 and YouTube Data API v3 operations.
- Hardwired constants:
  - `REDIRECT_URI`: `"http://localhost:8080/"`
  - `SCOPES`: `["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl"]`
- Functions:
  - `generate_auth_url(client_id, client_secret)`: Initiates OAuth flow with offline consent.
  - `exchange_code_for_tokens(flow, code_or_url)`: Extracts code and exchanges for tokens.
  - `fetch_user_subscriptions(client_id, client_secret, token, refresh_token)`: Retrieves channels with pagination and handles automatic token refreshing.
  - `find_or_create_playlist(client_id, client_secret, token, refresh_token, title)`: Finds or creates a private YouTube playlist (e.g. `YTSub Watch Later` or `YTSub Listen Later`).
  - `add_video_to_playlist(client_id, client_secret, token, refresh_token, playlist_id, video_id)`: Appends a video to the specified YouTube playlist.
  - `remove_video_from_playlist(client_id, client_secret, token, refresh_token, playlist_id, video_id)`: Finds and deletes items matching the video from the specified YouTube playlist.

### `rss.py`
- Performs non-blocking async HTTP queries to YouTube Atom feeds and custom RSS feeds.
- Functions:
  - `normalize_feed_url(input_str)`: Expands channel IDs (`UC...`) or YouTube channel/playlist URLs to valid RSS feed URLs.
  - `extract_feed_author(root)`: Extracts author/channel name from XML (`<author><name>` or `<author>`), falling back to `<title>`.
  - `parse_feed(xml_text)`: Parses Atom (`<entry>`) and RSS (`<item>`) feeds, extracting `video_id`, title, URL, and timestamps.
  - `check_channels_and_notify()`: Deduplicates and polls feed URLs across users, dispatching notifications formatted as `[{channel_title}] {url}` with `🕒 Watch Later` and `🎧 Listen Later` buttons.
- Supports `min_seconds_since_check` thresholding (used by `/reload` to filter feeds checked > 300s ago).
- Error tracking and alert dispatching: detects HTTP and XML parsing failures, records `error_count` and `last_error` in state, alerts the user once when `error_count` reaches 10, keeps counter at 10, and dispatches a recovery notification upon subsequent success.

### `handlers.py`
- Implements Telegram interactions using `python-telegram-bot` v21+:
  - `/start`: Interactive onboarding (runs OAuth 2.0 flow using server-configured Google credentials, checks for re-auth confirmation).
  - `/update`: Refresh subscriptions (errors with prompt to `/start` if not authenticated).
  - `/custom`: Manage custom RSS feeds (subcommands: `list`, `add <url_or_channel_id>`, `remove <number_or_url>`).
  - `/reload`: Admin-only command. Reloads state from disk and checks channels/feeds older than 5 minutes.
  - `/status`: Displays authenticated status, tracked channel count, custom feed count, check interval.
  - `/help`: Command summary (dynamically includes `/reload` only for admins).
  - Callback queries: Handles `reauth_*` confirmations, and `wl:*` / `ll:*` / `rwl:*` / `rll:*` playlist additions and removals with toggleable button states and toast confirmations.

### `main.py`
- Instantiates `ApplicationBuilder`, attaches handlers, and schedules periodic RSS polling via `job_queue.run_repeating()`.
- Filters out verbose `api.telegram.org` `getUpdates` HTTP polling requests from logs via `TelegramGetUpdatesFilter`.
- Sends startup notification to admin users.

---

## 4. State File Schema (`ytsub-state.json`)

```json
{
  "users": {
    "123456789": {
      "token": "ya29.a0...",
      "refresh_token": "1//0e...",
      "playlists": {
        "watch_later": "PL...",
        "listen_later": "PL..."
      },
      "channels": {
        "UC_x5XG1OV2P6uZZ5FSM9Ttw": {
          "title": "Google Developers",
          "last_published": "2026-09-13 10:00:00 UTC",
          "last_checked": "2026-09-13 11:25:00 UTC",
          "error_count": 0,
          "last_error": null
        }
      },
      "custom_feeds": {
        "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw": {
          "title": "Google Developers",
          "last_published": "2026-09-13 14:00:00 UTC",
          "last_checked": "2026-09-13 14:05:00 UTC",
          "error_count": 0,
          "last_error": null
        }
      }
    }
  }
}
```

*Note: Static constants (`token_uri`, `scopes`, `redirect_uri`, `check_interval_sec`) are not stored in the state file to maintain a clean and minimal schema.*

---

## 5. Development & Testing Conventions

- **Virtual Environment**: Local host execution must always use `.venv/`. Do not install packages into global Python.
- **Docker Usage**: Containers run system Python directly without a virtual environment.
- **Running Tests**: Run tests from the project root with `.venv/bin/pytest -v`.
- **Adding Commands**: Register new handlers in `handlers.py` and ensure unauthorized callers are intercepted by `check_access`.
- **Documentation Updates**: Both `README.md` and `AGENTS.md` must always be kept updated whenever features, commands, configurations, schemas, or architectural conventions are added or modified.
