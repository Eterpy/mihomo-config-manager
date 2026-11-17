import pytest
import asyncio
import jwt
import os
import subprocess
from pathlib import Path

from app.auth import AccessVerifier
from app.errors import AppError
from app.services import Store
from app.validation import validate_yaml


@pytest.mark.parametrize("tag", ["int", "float"])
def test_scalar_conversion_error_never_echoes_secrets(upload, tag, caplog, application):
    secret = "sensitive-node-password-fixture"
    response = upload(f"password: !!{tag} '{secret}'\n".encode())
    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text
    store = application.state.store
    assert store.list() == []
    assert not list(store.published.iterdir())
    assert not list(store.incoming.iterdir())


@pytest.mark.parametrize("path", ["/", "/api/configs", "/api/status", "/openapi.json", "/static/app.js", "/static/../settings.py"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_no_unauthenticated_admin_route(client, path, method):
    response = client.request(method, path, headers={"Cf-Access-Jwt-Assertion": "", "Cf-Access-Authenticated-User-Email": "owner@example.invalid"})
    assert response.status_code == 401


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_missing_signed_claim_denied(settings, keys, claim):
    claims = jwt.decode(keys.token(settings), options={"verify_signature": False})
    del claims[claim]
    token = jwt.encode(claims, keys.private, algorithm="RS256", headers={"kid": keys.kid, "jku": "https://attacker.invalid/keys", "jwk": keys.jwks()["keys"][0]})
    async def fetch_configured_keys():
        return keys.jwks()
    async def check():
        verifier = AccessVerifier(settings, fetcher=fetch_configured_keys)
        with pytest.raises(AppError) as exc:
            await verifier.verify(token)
        assert exc.value.status == 401
    asyncio.run(check())


def test_yaml_python_tag_cannot_execute(settings, tmp_path):
    marker = tmp_path / "must-not-exist"
    path = tmp_path / "unsafe.yaml"
    path.write_text(f"value: !!python/object/apply:os.system ['touch {marker}']")
    with pytest.raises(AppError):
        validate_yaml(path, settings)
    assert not marker.exists()


def test_successful_revocations_survive_restart(settings, tmp_path):
    source = tmp_path / "profile.yaml"
    source.write_bytes(b"proxies: []\n")
    store = Store(settings)
    disabled = store.create(source, "disabled.yaml", "disabled")
    deleted = store.create(source, "deleted.yaml", "deleted")
    rotated = store.create(source, "rotated.yaml", "rotated")
    store.change(disabled["id"], "disable")
    store.change(deleted["id"], "delete")
    new = store.change(rotated["id"], "rotate-token")
    store.close()
    recovered = Store(settings)
    try:
        for old in (disabled, deleted, rotated):
            assert not (recovered.published / (old["subscription_url"].split("/")[-2] + ".yaml")).exists()
        assert (recovered.published / (new["subscription_url"].split("/")[-2] + ".yaml")).read_bytes() == source.read_bytes()
        assert (recovered.private.stat().st_mode & 0o777) == 0o700
        assert (recovered.root.stat().st_mode & 0o777) == 0o700
    finally:
        recovered.close()


@pytest.mark.parametrize("version,allowed", [("27.5.1", False), ("28.0.0", True), ("29.1.0", True), ("unknown", False)])
def test_preflight_requires_safe_loopback_engine(tmp_path, version, allowed):
    original = Path(__file__).resolve().parents[1] / "scripts/preflight.sh"
    if not original.exists():
        pytest.skip("host deployment preflight tested outside container image")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "preflight.sh"
    script.write_bytes(original.read_bytes())
    (tmp_path / ".env").touch()
    for directory in ("db", "private", "published"):
        (tmp_path / "data" / directory).mkdir(parents=True)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\nif [ "$1" = "version" ]; then echo "$TEST_ENGINE_VERSION"; fi\n')
    docker.chmod(0o755)
    ss = commands / "ss"
    ss.write_text("#!/bin/sh\nexit 0\n")
    ss.chmod(0o755)
    result = subprocess.run(["sh", str(script)], env=dict(os.environ, PATH=f"{commands}:{os.environ['PATH']}", TEST_ENGINE_VERSION=version), capture_output=True)
    assert (result.returncode == 0) == allowed
