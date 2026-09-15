import json
import os
import tempfile
import pytest
from params import Params


def test_params_cli():
    p = Params()
    p.parse([
        "--bot-token", "123:ABC",
        "--admin-user-ids", "100,200",
        "--allowed-user-ids", "300",
        "--check-interval-sec", "600",
        "--subscription-sync-interval-sec", "7200",
        "--state-file", "test-state.json",
        "--google-client-id", "my-client-id",
        "--google-client-secret", "my-client-secret"
    ])
    assert p.bot_token == "123:ABC"
    assert 100 in p.admin_user_ids
    assert 200 in p.admin_user_ids
    assert 100 in p.allowed_user_ids
    assert 200 in p.allowed_user_ids
    assert 300 in p.allowed_user_ids
    assert p.check_interval_sec == 600
    assert p.check_interval == 600
    assert p.subscription_sync_interval_sec == 7200
    assert p.state_file == "test-state.json"
    assert p.google_client_id == "my-client-id"
    assert p.google_client_secret == "my-client-secret"
    assert p.is_user_admin(100)
    assert not p.is_user_admin(300)
    assert p.is_user_allowed(300)
    assert not p.is_user_allowed(999)


def test_params_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "456:DEF")
    monkeypatch.setenv("ADMIN_USERIDS", "500")
    monkeypatch.setenv("ALLOWED_USERIDS", "600")
    monkeypatch.setenv("CHECK_INTERVAL_SEC", "120")
    monkeypatch.setenv("SUBSCRIPTION_SYNC_INTERVAL_SEC", "1800")
    monkeypatch.setenv("STATE_FILE", "custom.json")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-client-secret")

    p = Params()
    p.parse([])
    assert p.bot_token == "456:DEF"
    assert 500 in p.admin_user_ids
    assert 600 in p.allowed_user_ids
    assert p.check_interval_sec == 120
    assert p.check_interval == 120
    assert p.subscription_sync_interval_sec == 1800
    assert p.state_file == "custom.json"
    assert p.google_client_id == "env-client-id"
    assert p.google_client_secret == "env-client-secret"


def test_params_client_secret_file():
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        json.dump({
            "installed": {
                "client_id": "file-client-id",
                "client_secret": "file-client-secret"
            }
        }, f)
        f_path = f.name

    try:
        p = Params()
        p.parse([
            "--bot-token", "123:test",
            "--google-client-secret-file", f_path
        ])
        assert p.google_client_id == "file-client-id"
        assert p.google_client_secret == "file-client-secret"
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_params_missing_token():
    p = Params()
    with pytest.raises(ValueError, match="bot token not set"):
        p.parse([])


def test_params_max_posts_per_min_cli():
    p = Params()
    p.parse([
        "--bot-token", "123:test",
        "--max-posts-per-min", "15"
    ])
    assert p.max_posts_per_min == 15

    # Test invalid value
    p_invalid = Params()
    with pytest.raises(ValueError, match="--max-posts-per-min must be >= 0"):
        p_invalid.parse([
            "--bot-token", "123:test",
            "--max-posts-per-min", "-1"
        ])


def test_params_max_posts_per_min_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("MAX_POSTS_PER_MIN", "25")
    p = Params()
    p.parse([])
    assert p.max_posts_per_min == 25


def test_params_subscription_sync_interval_validation(monkeypatch):
    p = Params()
    with pytest.raises(ValueError, match="--subscription-sync-interval-sec must be >= 0"):
        p.parse([
            "--bot-token", "123:test",
            "--subscription-sync-interval-sec", "-5"
        ])

    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("SUBSCRIPTION_SYNC_INTERVAL_SEC", "invalid")
    p2 = Params()
    with pytest.raises(ValueError, match="invalid subscription sync interval: invalid"):
        p2.parse([])


