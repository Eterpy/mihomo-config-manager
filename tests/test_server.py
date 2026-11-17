"""Signed test JWTs, injected JWKS; no ports are published by compose.test.yaml."""
from pathlib import Path
import os

from cryptography.hazmat.primitives import serialization

from app.auth import AccessVerifier
from app.main import create_app
from app.settings import Settings
from tests.helpers import TestKeys


def container_settings():
    return Settings(Path(os.getenv("MCM_TEST_DATA_DIR", "/data")), "test-team.cloudflareaccess.com", "test-audience", admin_hostname="admin:8000", public_base_url="https://cfg.test", admin_scheme="http")


def container_keys():
    keys = TestKeys()
    keys.private = serialization.load_pem_private_key(Path(os.getenv("MCM_TEST_KEY_PATH", "/fixtures/test-only-key.pem")).read_bytes(), password=None)
    return keys


def create_test_app():
    settings, keys = container_settings(), container_keys()
    async def fetch():
        return keys.jwks()
    return create_app(settings, AccessVerifier(settings, fetcher=fetch))
