import logging
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

REDIRECT_URI = "http://localhost:8080/"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
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
