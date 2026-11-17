import pytest


def published(store, item):
    return store.published / (item["subscription_url"].split("/")[-2] + ".yaml")


def test_full_lifecycle(upload, client, application):
    store = application.state.store
    first = upload().json()
    second = upload(filename="second.yml").json()
    assert first["subscription_url"] != second["subscription_url"]
    base = f'/api/configs/{first["id"]}'
    replacement = b"# new\nproxies: []\n"
    changed = client.put(base + "/content", files={"file": ("new.yaml", replacement)}).json()
    assert changed["subscription_url"] == first["subscription_url"]
    assert changed["version"] == 2
    assert published(store, first).read_bytes() == replacement
    assert client.post(base + "/disable").status_code == 200
    assert not published(store, first).exists()
    changed = client.put(base + "/content", files={"file": ("new.yaml", b"proxies: [ ]\n")}).json()
    assert not changed["enabled"] and not published(store, first).exists()
    rotated = client.post(base + "/rotate-token").json()
    assert rotated["subscription_url"] != first["subscription_url"]
    assert not published(store, rotated).exists()
    enabled = client.post(base + "/enable").json()
    assert enabled["subscription_url"] == rotated["subscription_url"]
    assert published(store, rotated).read_bytes() == b"proxies: [ ]\n"
    again = client.post(base + "/rotate-token").json()
    assert not published(store, rotated).exists()
    assert published(store, again).exists()
    assert published(store, second).read_bytes() == b"proxies: []\n"
    assert client.delete(base).status_code == 204
    assert not published(store, again).exists()
    assert not (store.private / first["id"]).exists()
    assert client.get(base).status_code == 404


def test_token_collision_retries(upload, monkeypatch):
    first = upload().json()
    old = first["subscription_url"].split("/")[-2]
    sequence = iter([old, "b" * 64])
    monkeypatch.setattr("app.services.secrets.token_hex", lambda _: next(sequence))
    second = upload(filename="second.yaml")
    assert second.status_code == 201
    assert "/" + "b" * 64 + "/" in second.json()["subscription_url"]


def test_collision_exhaustion(upload, monkeypatch):
    first = upload().json()
    old = first["subscription_url"].split("/")[-2]
    monkeypatch.setattr("app.services.secrets.token_hex", lambda _: old)
    assert upload(filename="second.yaml").status_code == 503


def test_metadata_rename(upload, client):
    first = upload().json()
    second = upload(filename="second.yaml").json()
    base = f'/api/configs/{first["id"]}'
    assert client.patch(base, json={"name": second["name"]}).status_code == 409
    renamed = client.patch(base, json={"name": " 新名称 ", "description": "说明"}).json()
    assert renamed["name"] == "新名称"
    assert renamed["subscription_url"] == first["subscription_url"]
    assert renamed["version"] == 1
    assert client.patch(base, json={"token": "bad"}).status_code == 422
