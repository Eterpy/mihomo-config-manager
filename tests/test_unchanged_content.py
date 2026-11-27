"""Identical content must not create versions or consume history slots."""
from dataclasses import replace

import pytest

from tests.test_tokens import published

A, B = b"rules: [A]\n", b"rules: [B]\n"


def put(client, item_id, data, version):
    return client.put(f"/api/configs/{item_id}/content?expected_current_version={version}",
                      files={"file": ("x.yaml", data)})


def versions(client, item_id):
    return client.get(f"/api/configs/{item_id}/versions").json()


def test_identical_upload_creates_no_version(upload, client, application):
    store = application.state.store
    item = upload(A).json()
    link = client.post(f"/api/configs/{item['id']}/subscription-link").json()["subscription_url"]
    public = store.published / (link.split("/")[-2] + ".yaml")
    before = public.stat()
    response = put(client, item["id"], A, 1)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "unchanged" and body["detail"] == "文件内容相同，未生成新版本"
    assert body["current"] == item
    # Nothing changed: metadata, revision, history, private files or the published copy.
    assert client.get(f"/api/configs/{item['id']}").json() == item
    assert [v["version"] for v in versions(client, item["id"])] == [1]
    assert {p.name for p in (store.private / item["id"] / "versions").iterdir()} == {"1.yaml"}
    assert public.stat().st_ino == before.st_ino and public.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(store.incoming.iterdir())
    assert store.ready and not store.failed


def test_identical_upload_keeps_history_slots(upload, client, application):
    store = application.state.store
    store.settings = replace(store.settings, history_versions=1)
    item = upload(A).json()
    assert put(client, item["id"], B, 1).status_code == 200
    for _ in range(3):
        assert put(client, item["id"], B, 2).json()["code"] == "unchanged"
    history = versions(client, item["id"])
    assert [v["version"] for v in history] == [2, 1]
    assert client.get(f"/api/configs/{item['id']}/versions/1/preview").content == A


def test_content_matching_history_is_still_allowed(upload, client, application):
    item = upload(A).json()
    assert put(client, item["id"], B, 1).status_code == 200
    again = put(client, item["id"], A, 2)
    assert again.status_code == 200 and again.json()["version"] == 3
    history = versions(client, item["id"])
    assert history[0]["sha256"] == history[2]["sha256"] and history[0]["source_version"] is None
    assert published(application.state.store, item).read_bytes() == A


def test_byte_level_change_is_a_new_version(upload, client):
    item = upload(A).json()
    response = put(client, item["id"], A.replace(b"\n", b"\r\n"), 1)
    assert response.status_code == 200 and response.json()["version"] == 2


def test_stale_revision_reported_before_unchanged(upload, client):
    item = upload(A).json()
    assert put(client, item["id"], B, 1).status_code == 200
    stale = put(client, item["id"], B, 1)
    assert stale.status_code == 409 and stale.json()["code"] == "stale"


def test_rollback_to_identical_content_is_rejected(upload, client):
    item = upload(A).json()
    base = f"/api/configs/{item['id']}"
    put(client, item["id"], B, 1)
    put(client, item["id"], A, 2)
    response = client.post(base + "/versions/1/rollback", json={"expected_current_version": 3})
    assert response.status_code == 409 and response.json()["code"] == "unchanged"
    assert client.get(base).json()["version"] == 3
    rolled = client.post(base + "/versions/2/rollback", json={"expected_current_version": 3})
    assert rolled.status_code == 200 and rolled.json()["version"] == 4
    assert versions(client, item["id"])[0]["source_version"] == 2


@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'symlink', 'directory'])
def test_upload_identical_bytes_repairs_current_file(upload, client, application, fault, tmp_path):
    item = upload(A).json()
    store = application.state.store
    path = store.private / item['id'] / 'versions/1.yaml'
    path.unlink()
    other = tmp_path / 'outside.yml'
    other.write_bytes(B)
    if fault == 'corrupt':
        path.write_bytes(B)
    elif fault == 'symlink':
        path.symlink_to(other)
    elif fault == 'directory':
        path.mkdir()
        (path / 'preserve').write_bytes(B)
    response = put(client, item['id'], A, 1)
    assert response.status_code == 200 and response.json()['version'] == 2
    assert not path.is_symlink() and path.read_bytes() == A and other.read_bytes() == B
    if fault == 'missing':
        assert not (store.root / 'quarantine').exists()
    elif fault == 'corrupt':
        saved, = (store.root / 'quarantine').rglob('1.yaml')
        assert saved.read_bytes() == B
    elif fault == 'symlink':
        saved, = (store.root / 'quarantine').rglob('1.yaml')
        assert saved.is_symlink() and saved.readlink() == other
    else:
        saved, = (store.root / 'quarantine').rglob('preserve')
        assert saved.read_bytes() == B
    store.recover()
    assert store.version_preview(item['id'], 1) == store.preview(item['id']) == A


def test_rollback_repairs_current_file_from_identical_history(upload, client, application):
    item = upload(A).json()
    ident = item['id']
    assert put(client, ident, B, 1).status_code == 200
    assert put(client, ident, A, 2).status_code == 200
    store = application.state.store
    (store.private / ident / 'versions/3.yaml').unlink()
    result = client.post(f'/api/configs/{ident}/versions/1/rollback', json={'expected_current_version': 3})
    assert result.status_code == 200 and result.json()['version'] == 4
    store.recover()
    assert store.version_preview(ident, 3) == store.preview(ident) == A


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_unchanged_republishes_current_file(upload, client, application, damage):
    item = upload(A).json()
    path = published(application.state.store, item)
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(B)
    response = put(client, item['id'], A, 1)
    assert response.status_code == 409 and response.json()['code'] == 'unchanged'
    assert path.read_bytes() == A and len(versions(client, item['id'])) == 1
