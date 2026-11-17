import re

import pytest
from fastapi.testclient import TestClient

from app.auth import AccessVerifier
from app.main import create_app
from app.settings import Settings
from tests.helpers import TestKeys


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "data", "test-team.cloudflareaccess.com", "test-audience", admin_hostname="files.test", public_base_url="https://cfg.test")


@pytest.fixture(scope="session")
def keys():
    return TestKeys()


@pytest.fixture
def application(settings, keys):
    async def fetch():
        return keys.jwks()
    return create_app(settings, AccessVerifier(settings, fetcher=fetch))


@pytest.fixture
def client(application, settings, keys):
    with TestClient(application, base_url=settings.origin, headers={"Cf-Access-Jwt-Assertion": keys.token(settings)}) as client:
        page = client.get("/")
        assert page.status_code == 200
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        client.headers.update({"Origin": settings.origin, "X-CSRF-Token": token})
        yield client


@pytest.fixture
def upload(client):
    def send(content=b"proxies: []\n", filename="test.yaml", name=None):
        return client.post("/api/configs", files={"file": (filename, content, "text/yaml")}, data={"name": name} if name is not None else {})
    return send
