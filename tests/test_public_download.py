import concurrent.futures
import http.client
import os
import re
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from tests.helpers import link, revision
from tests.test_server import container_keys, container_settings

pytestmark = [pytest.mark.container, pytest.mark.skipif(os.getenv("MCM_CONTAINER_TEST") != "1", reason="run scripts/test-containers.sh on Docker host")]


@pytest.fixture
def remote():
    settings, keys = container_settings(), container_keys()
    with httpx.Client(base_url=os.getenv("MCM_TEST_ADMIN_URL", "http://admin:8000"), headers={"Host": "admin:8000", "Cf-Access-Jwt-Assertion": keys.token(settings)}, timeout=15, trust_env=False) as client:
        page = client.get("/")
        assert page.status_code == 200
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        client.headers.update({"Origin": settings.origin, "X-CSRF-Token": token})
        yield client


def request_public(path, method="GET", headers=None):
    # http.client preserves dot segments: test raw requests, not client normalization.
    conn = http.client.HTTPConnection(os.getenv("MCM_TEST_PUBLIC_HOST", "public"), int(os.getenv("MCM_TEST_PUBLIC_PORT", "8080")), timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
    finally:
        conn.close()


def create(remote, data=b"# original\r\nproxies: []\r\n"):
    response = remote.post("/api/configs", files={"file": (f"{uuid.uuid4()}.yaml", data)})
    assert response.status_code == 201
    return response.json()


def test_live_download_lifecycle(remote):
    content = b"\xef\xbb\xbf# original\r\nproxies: []\r\n"
    item = create(remote, content)
    second = create(remote)
    path = urlsplit(link(remote, item)).path
    status, headers, body = request_public(path)
    assert status == 200 and body == content
    assert headers["content-type"] == "text/yaml; charset=utf-8"
    assert headers["cache-control"] == "private, no-store, max-age=0"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"
    assert "etag" not in headers
    status, head_headers, body = request_public(path, "HEAD")
    assert status == 200 and not body and int(head_headers["content-length"]) == len(content)
    base = f'/api/configs/{item["id"]}'
    updated = b"# changed\nproxies: []\n"
    response = remote.put(base + "/content", params={"expected_current_version": 1}, files={"file": ("new.yaml", updated)})
    assert response.status_code == 200
    assert urlsplit(link(remote, response.json())).path == path
    status, _, body = request_public(path, headers={"If-Modified-Since": "Wed, 31 Dec 2098 23:59:59 GMT", "If-None-Match": '"old"'})
    assert status == 200 and body == updated
    assert remote.post(base + "/disable", params=revision(remote, base)).status_code == 200
    assert request_public(path)[0] == 404
    assert remote.post(base + "/enable", params=revision(remote, base)).status_code == 200
    assert request_public(path)[0] == 200
    rotated = remote.post(base + "/rotate-token", params=revision(remote, base)).json()
    new_path = urlsplit(link(remote, rotated)).path
    assert request_public(path)[0] == 404
    assert request_public(path, "HEAD")[0] == 404
    assert request_public(new_path)[2] == updated
    assert request_public(urlsplit(link(remote, second)).path)[0] == 200
    assert remote.delete(base, params=revision(remote, base)).status_code == 204
    assert request_public(new_path)[0] == 404
    assert remote.delete(f'/api/configs/{second["id"]}', params=revision(remote, f'/api/configs/{second["id"]}')).status_code == 204


def test_live_paths_methods_and_auth(remote):
    item = create(remote)
    path = urlsplit(link(remote, item)).path
    for invalid in ["/", "/api", "/data", "/.env", "/../", "/healthz", "/data/db/manager.sqlite3", "/private/" + item["id"] + "/versions/1.yaml", "/published/" + path.split("/")[-2] + ".yaml", path.upper(), "/s/" + "a" * 63 + "/profile.yaml", "/s/" + "0" * 64 + "/profile.yaml", path + "/", path + "?k=x", path.replace("/s/", "//s/"), path.replace("/s/", "/%73/"), "/extra/.." + path, path.replace("/profile", "/%70rofile")]:
        assert request_public(invalid)[0] == 404
    for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
        assert request_public(path, method)[0] == 404
    assert remote.get("/api/configs", headers={"Cf-Access-Jwt-Assertion": ""}).status_code == 401
    assert remote.get("/healthz").status_code == (200 if os.getenv("MCM_TEST_ADMIN_URL") else 404)
    assert remote.delete(f'/api/configs/{item["id"]}', params=revision(remote, f'/api/configs/{item["id"]}')).status_code == 204


def test_live_replace_concurrent_downloads(remote):
    old, new = b"value: " + b"a" * 100000, b"value: " + b"b" * 200000
    item = create(remote, old)
    path = urlsplit(link(remote, item)).path
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(lambda: [request_public(path) for _ in range(20)]) for _ in range(3)]
        for index in range(4):
            response = remote.put(f'/api/configs/{item["id"]}/content', params={'expected_current_version': index + 1}, files={"file": ("new.yaml", new if index % 2 == 0 else old)})
            assert response.status_code == 200
        for job in jobs:
            assert all(status == 200 and body in {old, new} for status, _, body in job.result())
    remote.delete(f'/api/configs/{item["id"]}', params=revision(remote, f'/api/configs/{item["id"]}'))


def test_public_path_encoding_never_exposes_profile(remote):
    content = b"password: synthetic-private-canary\n"
    item = create(remote, content)
    path = urlsplit(link(remote, item)).path
    token = path.split("/")[2]
    invalid_paths = [
        path + ".bak", path + "%00", path + "%0a", path + ";ignored",
        path + "?download=1", path.replace("/s/", "/%2573/"),
        path.replace("/s/", "/s%2f"), path.replace("/s/", "/s/./"),
        path.replace("/s/", "/s/%2e/"), path.replace("/s/", "/s/%2e%2e/s/"),
        f"/s/{token}/../../db/manager.sqlite3", f"/s/{token}/%2e%2e/profile.yaml",
        f"/s/{token}/profile%2eyaml", f"/s/{token}/versions/1.yaml",
        f"/{token}.yaml", "/private/.incoming/", "/.git/config", "/backups/",
    ]
    try:
        for index, invalid in enumerate(invalid_paths):
            for method in ("GET", "HEAD"):
                status, headers, body = request_public(invalid, method)
                assert status == 404, f"invalid-path case {index}, {method}: HTTP {status}"
                assert b"synthetic-private-canary" not in body
                assert "no-store" in headers["cache-control"]
    finally:
        remote.delete(f'/api/configs/{item["id"]}', params=revision(remote, f'/api/configs/{item["id"]}'))


def test_public_never_trusts_identity_headers(remote):
    item = create(remote)
    try:
        headers = {"Cf-Access-Jwt-Assertion": remote.headers["Cf-Access-Jwt-Assertion"], "Cf-Access-Authenticated-User-Email": "owner@example.invalid", "X-Original-URL": urlsplit(link(remote, item)).path, "X-Rewrite-URL": urlsplit(link(remote, item)).path}
        for invalid in ("/api/configs", "/data/db/manager.sqlite3", "/private/", "/s/invalid/profile.yaml"):
            assert request_public(invalid, headers=headers)[0] == 404
    finally:
        remote.delete(f'/api/configs/{item["id"]}', params=revision(remote, f'/api/configs/{item["id"]}'))


def test_live_history_and_backup_isolation(remote):
    import time
    item = create(remote, b'original: true\n')
    base = '/api/configs/' + item['id']
    path = urlsplit(link(remote, item)).path
    assert remote.put(base + '/content', params={'expected_current_version': 1}, files={'file': ('new.yaml', b'new: true\n')}).status_code == 200
    assert remote.post(base + '/versions/1/rollback', json={'expected_current_version': 2}).status_code == 200
    assert request_public(path)[2] == b'original: true\n'
    response = remote.post('/api/backups', json={'password': 'isolated-test-passphrase'})
    assert response.status_code == 202
    ident = response.json()['id']
    for _ in range(100):
        assert request_public(path)[0] == 200
        row = remote.get('/api/backups/' + ident).json()
        if row['status'] in {'ready', 'failed'}: break
        time.sleep(.05)
    assert row['status'] == 'ready'
    for candidate in [f'/backups/{ident}.mcmbackup', f'/api/backups/{ident}/download', f'/private/{item["id"]}/versions/1.yaml']:
        assert request_public(candidate)[0] == 404
    assert remote.get(f'/api/backups/{ident}/download').content.startswith(b'MCMBAK01')
    assert remote.delete('/api/backups/' + ident).status_code == 204
    assert remote.delete(base, params=revision(remote, base)).status_code == 204
