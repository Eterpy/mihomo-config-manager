import asyncio

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import AccessVerifier, CSRF
from app.errors import AppError
from app.settings import Settings
from tests.helpers import TestKeys


@pytest.mark.parametrize("route", ["/", "/api/configs", "/api/status", "/openapi.json", "/static/app.js"])
def test_auth_every_route(client, route):
    assert client.get(route, headers={"Cf-Access-Jwt-Assertion": ""}).status_code == 401
    assert client.get(route, headers={"Cf-Access-Jwt-Assertion": "forged", "Cf-Access-Authenticated-User-Email": "admin@example.invalid"}).status_code == 401


@pytest.mark.parametrize("override", [{"aud": ["wrong"]}, {"iss": "https://wrong.cloudflareaccess.com"}, {"exp": 1}, {"iat": 9999999999}, {"nbf": 9999999999}, {"sub": ""}])
def test_invalid_claims(client, settings, keys, override):
    assert client.get("/api/configs", headers={"Cf-Access-Jwt-Assertion": keys.token(settings, **override)}).status_code == 401


def test_wrong_signature_and_algorithm(client, settings):
    other = TestKeys()
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": other.token(settings)}).status_code == 401
    token = jwt.encode({"sub": "admin"}, "test" * 16, algorithm="HS256", headers={"kid": "test-key"})
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 401


def test_csrf_and_origin(client):
    for headers in [{"Origin": "https://evil.test"}, {"Origin": ""}, {"X-CSRF-Token": "bad"}, {"X-CSRF-Token": ""}]:
        response = client.post("/api/configs", headers=headers)
        assert response.status_code == 403
    assert client.get("/api/configs", headers={"Host": "evil.test"}).status_code == 400


def test_csrf_identity_expiry_and_restart():
    first = CSRF()
    token = first.issue("admin")
    assert first.verify("admin", token)
    assert not first.verify("other", token)
    assert not first.verify("admin", "1." + token.split(".", 1)[1])
    assert not CSRF().verify("admin", token)


def test_jwks_rotation_and_throttle(settings, keys):
    other = TestKeys()
    other.kid = "new-key"
    calls = []
    async def fetch():
        calls.append(True)
        return keys.jwks() if len(calls) == 1 else other.jwks()
    async def check():
        verifier = AccessVerifier(settings, fetcher=fetch, refresh_interval=0)
        assert (await verifier.verify(keys.token(settings)))["sub"] == "test-admin"
        await verifier.verify(keys.token(settings))
        assert len(calls) == 1
        await verifier.verify(other.token(settings))
        assert len(calls) == 2
        verifier.refresh_interval = 30
        with pytest.raises(AppError) as error:
            await verifier.verify(keys.token(settings))
        assert error.value.status == 401
        assert len(calls) == 2
    asyncio.run(check())


def test_jwks_failure_closed(settings, keys):
    async def fail():
        raise OSError("test network failure")
    async def check():
        verifier = AccessVerifier(settings, fetcher=fail)
        with pytest.raises(AppError) as error:
            await verifier.verify(keys.token(settings))
        assert error.value.status == 503
    asyncio.run(check())


def test_production_requires_settings_and_rejects_bypass(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "test.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "test-aud")
    monkeypatch.setenv("ADMIN_HOSTNAME", "admin.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://subscribe.test")
    assert Settings.from_env().issuer == "https://test.cloudflareaccess.com"
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("APP_ENV", "development")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_health_is_local_only(application, settings):
    with TestClient(application, base_url=settings.origin) as external:
        assert external.get("/healthz").status_code == 404
    with TestClient(application, base_url=settings.origin, client=("127.0.0.1", 1234)) as local:
        assert local.get("/healthz").status_code == 200


def test_cookie_headers_and_identity_binding(client, settings, keys):
    response = client.get("/")
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.post("/api/configs", headers={"Cf-Access-Jwt-Assertion": keys.token(settings, sub="another-user")}).status_code == 403
