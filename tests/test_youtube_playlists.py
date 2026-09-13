from unittest.mock import MagicMock, patch
import pytest

import youtube


def test_find_or_create_playlist_found():
    with patch("youtube.build") as mock_build, patch("youtube.get_credentials") as mock_creds:
        mock_creds_obj = MagicMock()
        mock_creds_obj.token = "tok1"
        mock_creds.return_value = mock_creds_obj

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        # Mock list response finding the playlist
        mock_list_request = MagicMock()
        mock_list_request.execute.return_value = {
            "items": [
                {"id": "PL_other", "snippet": {"title": "Other Playlist"}},
                {"id": "PL_target_123", "snippet": {"title": "YTSub Watch Later"}}
            ],
            "nextPageToken": None
        }
        mock_service.playlists().list.return_value = mock_list_request

        pid, refreshed = youtube.find_or_create_playlist(
            client_id="cid",
            client_secret="csec",
            token="tok1",
            refresh_token="ref1",
            title="YTSub Watch Later"
        )

        assert pid == "PL_target_123"
        assert refreshed is None
        mock_service.playlists().insert.assert_not_called()


def test_find_or_create_playlist_create_new():
    with patch("youtube.build") as mock_build, patch("youtube.get_credentials") as mock_creds:
        mock_creds_obj = MagicMock()
        mock_creds_obj.token = "refreshed_tok"
        mock_creds.return_value = mock_creds_obj

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        # Empty list
        mock_list_request = MagicMock()
        mock_list_request.execute.return_value = {"items": [], "nextPageToken": None}
        mock_service.playlists().list.return_value = mock_list_request

        # Insert response
        mock_insert_request = MagicMock()
        mock_insert_request.execute.return_value = {"id": "PL_new_created"}
        mock_service.playlists().insert.return_value = mock_insert_request

        pid, refreshed = youtube.find_or_create_playlist(
            client_id="cid",
            client_secret="csec",
            token="old_tok",
            refresh_token="ref1",
            title="YTSub Listen Later"
        )

        assert pid == "PL_new_created"
        assert refreshed == "refreshed_tok"
        mock_service.playlists().insert.assert_called_once()
        args, kwargs = mock_service.playlists().insert.call_args
        assert kwargs["body"]["snippet"]["title"] == "YTSub Listen Later"
        assert kwargs["body"]["status"]["privacyStatus"] == "private"


def test_add_video_to_playlist():
    with patch("youtube.build") as mock_build, patch("youtube.get_credentials") as mock_creds:
        mock_creds_obj = MagicMock()
        mock_creds_obj.token = "tok1"
        mock_creds.return_value = mock_creds_obj

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_insert = MagicMock()
        mock_insert.execute.return_value = {"id": "item_123"}
        mock_service.playlistItems().insert.return_value = mock_insert

        success, refreshed = youtube.add_video_to_playlist(
            client_id="cid",
            client_secret="csec",
            token="tok1",
            refresh_token="ref1",
            playlist_id="PL_target",
            video_id="video_999"
        )

        assert success is True
        assert refreshed is None
        mock_service.playlistItems().insert.assert_called_once()
        args, kwargs = mock_service.playlistItems().insert.call_args
        body = kwargs["body"]
        assert body["snippet"]["playlistId"] == "PL_target"
        assert body["snippet"]["resourceId"]["videoId"] == "video_999"


def test_remove_video_from_playlist():
    with patch("youtube.build") as mock_build, patch("youtube.get_credentials") as mock_creds:
        mock_creds_obj = MagicMock()
        mock_creds_obj.token = "tok1"
        mock_creds.return_value = mock_creds_obj

        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {"id": "item_to_delete_1"},
                {"id": "item_to_delete_2"}
            ]
        }
        mock_service.playlistItems().list.return_value = mock_list

        mock_delete = MagicMock()
        mock_service.playlistItems().delete.return_value = mock_delete

        success, refreshed = youtube.remove_video_from_playlist(
            client_id="cid",
            client_secret="csec",
            token="tok1",
            refresh_token="ref1",
            playlist_id="PL_target",
            video_id="video_999"
        )

        assert success is True
        assert refreshed is None
        mock_service.playlistItems().list.assert_called_once_with(
            part="id",
            playlistId="PL_target",
            videoId="video_999",
            maxResults=50
        )
        assert mock_service.playlistItems().delete.call_count == 2
        mock_service.playlistItems().delete.assert_any_call(id="item_to_delete_1")
        mock_service.playlistItems().delete.assert_any_call(id="item_to_delete_2")

