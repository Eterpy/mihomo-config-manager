import concurrent.futures
import sys
import threading
from dataclasses import replace

import pytest

from app.errors import AppError
from app.validation import validate_yaml


def test_upload_concurrency_limit(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked(path, settings):
        entered.set()
        assert release.wait(5)
        validate_yaml(path, settings)
    monkeypatch.setattr("app.main.validate_yaml", blocked)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/configs", files={"file": ("first.yaml", b"x: 1")})
        try:
            assert entered.wait(5)
            second = client.post("/api/configs", files={"file": ("second.yaml", b"x: 2")})
            assert second.status_code == 429
            assert second.headers["Retry-After"] == "2"
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 201
    assert client.post("/api/configs", files={"file": ("after.yaml", b"x: 3")}).status_code == 201


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on production Linux")
def test_linux_yaml_memory_budget(settings, tmp_path):
    path = tmp_path / "complex.yaml"
    path.write_bytes(b"items: [" + b"0," * 200000 + b"0]\n")
    with pytest.raises(AppError) as error:
        validate_yaml(path, replace(settings, yaml_memory_mb=64, yaml_timeout=10))
    assert error.value.status == 422


def test_forwarded_headers_cannot_make_health_local(client):
    response = client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
    assert response.status_code == 404


def test_openapi_auth_and_multipart(client):
    document = client.get("/openapi.json").json()
    assert document["security"] == [{"AccessJWT": []}]
    upload = document["paths"]["/api/configs"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert upload["security"][0] == {"AccessJWT": [], "CSRFHeader": [], "CSRFCookie": []}


def test_logs_do_not_contain_yaml_or_tokens(client, upload, application, monkeypatch, caplog):
    item = upload(b"secret: do-not-log-fixture\n").json()
    token = item["subscription_url"].split("/")[-2]
    def fail(row):
        raise OSError(f"token {token} do-not-log-fixture")
    monkeypatch.setattr(application.state.store, "_publish", fail)
    assert client.post(f'/api/configs/{item["id"]}/rotate-token').status_code == 503
    assert token not in caplog.text and "do-not-log-fixture" not in caplog.text
    assert "storage_operation_pending" in caplog.text
