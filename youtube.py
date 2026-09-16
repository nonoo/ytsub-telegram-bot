import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

if TYPE_CHECKING:
    from params import Params
    from state import StateManager

logger = logging.getLogger(__name__)

REDIRECT_URI = "http://localhost:8080/"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def make_client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [REDIRECT_URI]
        }
    }


def create_oauth_flow(client_id: str, client_secret: str) -> Flow:
    config = make_client_config(client_id, client_secret)
    flow = Flow.from_client_config(config, scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    return flow


def generate_auth_url(client_id: str, client_secret: str) -> Tuple[str, Flow]:
    flow = create_oauth_flow(client_id, client_secret)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    return auth_url, flow


def extract_code_from_input(user_input: str) -> str:
    user_input = user_input.strip()
    if user_input.startswith("http://") or user_input.startswith("https://"):
        parsed = urlparse(user_input)
        params = parse_qs(parsed.query)
        code = params.get("code")
        if code:
            return code[0]
    return user_input


def exchange_code_for_tokens(flow: Flow, code_or_url: str) -> Tuple[str, str]:
    code = extract_code_from_input(code_or_url)
    flow.fetch_token(code=code)
    creds = flow.credentials
    token = creds.token or ""
    refresh_token = creds.refresh_token or ""
    return token, refresh_token


def get_credentials(client_id: str, client_secret: str, token: str, refresh_token: str) -> Credentials:
    return Credentials(
        token=token,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES
    )


def fetch_user_subscriptions(client_id: str, client_secret: str, token: str, refresh_token: str) -> Tuple[Dict[str, str], Optional[str]]:
    """
    Returns (channels_dict, new_access_token_if_refreshed).
    channels_dict: {channel_id: channel_title}
    """
    creds = get_credentials(client_id, client_secret, token, refresh_token)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    channels: Dict[str, str] = {}
    next_page_token = None

    while True:
        request = service.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            resource_id = snippet.get("resourceId", {})
            channel_id = resource_id.get("channelId")
            title = snippet.get("title", "")
            if channel_id:
                channels[channel_id] = title

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    refreshed_token = creds.token if creds.token != token else None
    return channels, refreshed_token


def find_or_create_playlist(
    client_id: str,
    client_secret: str,
    token: str,
    refresh_token: str,
    title: str
) -> Tuple[str, Optional[str]]:
    """
    Finds a playlist by exact title in the user's account, or creates it as a private playlist.
    Returns (playlist_id, new_access_token_if_refreshed).
    """
    creds = get_credentials(client_id, client_secret, token, refresh_token)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    next_page_token = None
    while True:
        request = service.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=next_page_token
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            if snippet.get("title") == title:
                playlist_id = item.get("id")
                refreshed_token = creds.token if creds.token != token else None
                return playlist_id, refreshed_token

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    # If not found, create it as a private playlist
    insert_request = service.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": "Created by YTSub Telegram Bot"
            },
            "status": {
                "privacyStatus": "private"
            }
        }
    )
    insert_response = insert_request.execute()
    playlist_id = insert_response.get("id")
    refreshed_token = creds.token if creds.token != token else None
    return playlist_id, refreshed_token


def add_video_to_playlist(
    client_id: str,
    client_secret: str,
    token: str,
    refresh_token: str,
    playlist_id: str,
    video_id: str
) -> Tuple[bool, Optional[str]]:
    """
    Inserts a video into the specified playlist.
    Returns (success, new_access_token_if_refreshed).
    """
    creds = get_credentials(client_id, client_secret, token, refresh_token)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    request = service.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
    )
    request.execute()
    refreshed_token = creds.token if creds.token != token else None
    return True, refreshed_token


def remove_video_from_playlist(
    client_id: str,
    client_secret: str,
    token: str,
    refresh_token: str,
    playlist_id: str,
    video_id: str
) -> Tuple[bool, Optional[str]]:
    """
    Finds and deletes any playlist item containing video_id from playlist_id.
    Returns (success, new_access_token_if_refreshed).
    """
    if not playlist_id:
        return True, None

    creds = get_credentials(client_id, client_secret, token, refresh_token)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    request = service.playlistItems().list(
        part="id",
        playlistId=playlist_id,
        videoId=video_id,
        maxResults=50
    )
    response = request.execute()

    for item in response.get("items", []):
        item_id = item.get("id")
        if item_id:
            service.playlistItems().delete(id=item_id).execute()

    refreshed_token = creds.token if creds.token != token else None
    return True, refreshed_token


def format_channel_delta_log(added_titles: List[str], removed_titles: List[str]) -> Optional[str]:
    """
    Formats the delta of added and removed channel titles for logging.
    If there are more than 10 channels in a row, adds ', and X more'.
    Returns None if neither added nor removed channels exist.
    """
    lines = []
    if added_titles:
        added_str = ", ".join(added_titles[:10]) + (f", and {len(added_titles) - 10} more" if len(added_titles) > 10 else "")
        lines.append(f"Added channels: {added_str}")
    if removed_titles:
        removed_str = ", ".join(removed_titles[:10]) + (f", and {len(removed_titles) - 10} more" if len(removed_titles) > 10 else "")
        lines.append(f"Removed channels: {removed_str}")
    if lines:
        return "\n".join(lines)
    return None


async def sync_user_subscriptions(
    state: "StateManager",
    params: "Params",
    user_id: int,
    send_message_fn: Optional[Callable[[int, str], Awaitable[Any]]] = None,
) -> Tuple[int, int]:
    """
    Synchronizes YouTube channel subscriptions for a given user.
    Returns (total_channels_count, newly_added_count).
    """
    client_id = params.google_client_id
    client_secret = params.google_client_secret
    user_info = state.get_user(user_id)
    token = user_info.get("token", "")
    refresh_token = user_info.get("refresh_token", "")

    if not (token or refresh_token):
        raise ValueError(f"User {user_id} does not have OAuth credentials.")

    channels, refreshed_token = await asyncio.to_thread(
        fetch_user_subscriptions, client_id, client_secret, token, refresh_token
    )
    if refreshed_token:
        state.set_user_tokens(user_id, token=refreshed_token, refresh_token=refresh_token)

    existing_channels = user_info.get("channels", {})
    added_titles = [title for ch_id, title in channels.items() if ch_id not in existing_channels]
    removed_titles = [
        ch_data.get("title") or ch_id
        for ch_id, ch_data in existing_channels.items()
        if ch_id not in channels
    ]

    new_count = state.sync_user_channels(user_id, channels)

    delta_msg = format_channel_delta_log(added_titles, removed_titles)
    if delta_msg:
        logger.info("%s", delta_msg)
        if send_message_fn:
            try:
                await send_message_fn(user_id, delta_msg)
            except Exception as e:
                logger.warning("Failed to send subscription update message to user %s: %s", user_id, e)

    return len(channels), new_count


async def sync_all_subscriptions(
    state: "StateManager",
    params: "Params",
    send_message_fn: Optional[Callable[[int, str], Awaitable[Any]]] = None,
) -> Dict[int, Tuple[int, int]]:
    """
    Syncs subscriptions for all allowed users with OAuth credentials.
    Returns a dict mapping user_id -> (total_channels, newly_added_count).
    """
    if not params.google_client_id or not params.google_client_secret:
        logger.warning("Google OAuth credentials are not configured. Skipping subscription sync.")
        return {}

    results = {}
    users_data = state.data.get("users", {})
    for uid_str, user_info in list(users_data.items()):
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        if not params.is_user_allowed(uid):
            continue

        if not (user_info.get("token") or user_info.get("refresh_token")):
            continue

        try:
            total, new_count = await sync_user_subscriptions(
                state, params, uid, send_message_fn=send_message_fn
            )
            results[uid] = (total, new_count)
            logger.info("Synced subscriptions for user %d: %d total (%d newly added).", uid, total, new_count)
        except Exception as e:
            logger.error("Failed to sync subscriptions for user %d: %s", uid, e)

    return results



