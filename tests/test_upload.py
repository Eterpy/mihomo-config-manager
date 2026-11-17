import hashlib
import re
import subprocess

import pytest

from app.errors import AppError
from app.validation import validate_yaml


@pytest.mark.parametrize("content", [b"# comment\nproxies: []\n", b"\xef\xbb\xbf# BOM\r\nproxies: []\r\n", '说明: 中文\nproxies: []\n'.encode()])
def test_original_bytes(upload, application, content):
    response = upload(content)
    assert response.status_code == 201, response.text
    item = response.json()
    token = item["subscription_url"].split("/")[-2]
    assert re.fullmatch("[0-9a-f]{64}", token)
    assert item["sha256"] == hashlib.sha256(content).hexdigest()
    assert (application.state.store.published / f"{token}.yaml").read_bytes() == content


@pytest.mark.parametrize("content,filename", [(b"", "x.yaml"), (b"foo: \xff\n", "x.yaml"), (b"x: \x00\n", "x.yaml"), (b"x: \x01\n", "x.yaml"), (b"- item\n", "x.yml"), (b"scalar", "x.yaml"), (b"x: [", "x.yaml"), (b"# comments only", "x.yml"), (b"x: 1\n---\ny: 1", "x.yml"), (b"!!python/object/apply:os.system ['echo unsafe']", "x.yaml"), (b"x: 1", "x.txt"), (b"x: 1", "../x.yaml"), (b"x: 1", "/x.yaml"), (b"x: 1", "C:\\x.yaml")])
def test_reject_invalid(upload, content, filename):
    response = upload(content, filename)
    assert response.status_code == 422, response.text


def test_oversize_and_duplicate(upload, client):
    assert upload(b"x: " + b"a" * (10 * 1024 * 1024)).status_code == 413
    assert upload(filename="same.yaml").status_code == 201
    assert upload(filename="same.yaml").status_code == 409
    assert upload(filename="same.yaml", name=" another ").json()["name"] == "another"
    assert upload(filename="new.yaml", name=" ").status_code == 422
    assert len(client.get("/api/configs").json()) == 2


def test_multipart_count_and_body_limit(client):
    assert client.post("/api/configs", files=[("file", ("a.yaml", b"x: 1")), ("file", ("b.yaml", b"x: 2"))]).status_code == 400
    limit = 10 * 1024 * 1024 + 65536
    response = client.post("/api/configs", headers={"Content-Type": "multipart/form-data; boundary=X"}, content=(part for part in [b"--X\r\n", b"x" * (limit + 1)]))
    assert response.status_code == 413
    assert client.get("/api/configs").json() == []


def test_safe_preview(upload, client):
    content = b'value: "<script>alert(1)</script>"\n'
    item = upload(content, filename="<script>.yaml").json()
    response = client.get(f'/api/configs/{item["id"]}/preview')
    assert response.content == content
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<script>.yaml" not in client.get("/").text


def test_parser_timeout_and_crash(monkeypatch, settings, tmp_path):
    path = tmp_path / "test.yaml"
    path.write_bytes(b"x: 1")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("parser", 0.01)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AppError) as error:
        validate_yaml(path, settings)
    assert error.value.status == 422
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], -9, b"", b""))
    with pytest.raises(AppError) as error:
        validate_yaml(path, settings)
    assert error.value.status == 422


def test_no_yaml_content_in_validation_error(upload):
    response = upload(b"secret-value: [never-log-this-secret\n")
    assert response.status_code == 422
    assert "never-log-this-secret" not in response.text
