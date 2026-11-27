from tests.helpers import link, revision


def published(store, item):
    return store.published / (store.subscription_url(item["id"]).split("/")[-2] + ".yaml")


def test_full_lifecycle(upload, client, application):
    store = application.state.store
    first = upload().json()
    second = upload(filename="second.yml").json()
    original = link(client, first)
    original_file = published(store, first)
    assert original != link(client, second)
    base = f'/api/configs/{first["id"]}'
    replacement = b"# new\nproxies: []\n"
    changed = client.put(base + "/content", params={"expected_current_version": 1}, files={"file": ("new.yaml", replacement)}).json()
    assert link(client, changed) == original
    assert changed["version"] == 2
    assert published(store, first).read_bytes() == replacement
    assert client.post(base + "/disable", params=revision(client, base)).status_code == 200
    assert not original_file.exists()
    changed = client.put(base + "/content", params={"expected_current_version": 2}, files={"file": ("new.yaml", b"proxies: [ ]\n")}).json()
    assert not changed["enabled"] and not published(store, first).exists()
    rotated = client.post(base + "/rotate-token", params=revision(client, base)).json()
    rotated_url = link(client, rotated)
    rotated_file = published(store, rotated)
    assert rotated_url != original
    assert not rotated_file.exists()
    enabled = client.post(base + "/enable", params=revision(client, base)).json()
    assert link(client, enabled) == rotated_url
    assert published(store, rotated).read_bytes() == b"proxies: [ ]\n"
    again = client.post(base + "/rotate-token", params=revision(client, base)).json()
    assert not rotated_file.exists()
    assert published(store, again).exists()
    assert published(store, second).read_bytes() == b"proxies: []\n"
    new_file = published(store, again)
    assert client.delete(base, params=revision(client, base)).status_code == 204
    assert not new_file.exists()
    assert not (store.private / first["id"]).exists()
    assert client.get(base).status_code == 404


def test_token_collision_retries(upload, monkeypatch, client):
    first = upload().json()
    old = link(client, first).split("/")[-2]
    sequence = iter([old, "b" * 64])
    monkeypatch.setattr("app.services.secrets.token_hex", lambda _: next(sequence))
    second = upload(filename="second.yaml")
    assert second.status_code == 201
    assert "/" + "b" * 64 + "/" in link(client, second.json())


def test_collision_exhaustion(upload, monkeypatch, client):
    first = upload().json()
    old = link(client, first).split("/")[-2]
    monkeypatch.setattr("app.services.secrets.token_hex", lambda _: old)
    assert upload(filename="second.yaml").status_code == 503


def test_metadata_rename(upload, client):
    first = upload().json()
    second = upload(filename="second.yaml").json()
    base = f'/api/configs/{first["id"]}'
    original = link(client, first)
    assert client.patch(base, json={**revision(client, base), "name": second["name"]}).status_code == 409
    renamed = client.patch(base, json={**revision(client, base), "name": " 新名称 ", "description": "说明"}).json()
    assert renamed["name"] == "新名称"
    assert link(client, renamed) == original
    assert renamed["version"] == 1
    assert client.patch(base, json={**revision(client, base), "token": "bad"}).status_code == 422
