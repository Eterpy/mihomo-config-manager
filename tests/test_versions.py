def test_current_plus_five_history(upload, client, application):
    item = upload().json()
    for version in range(2, 9):
        response = client.put(f'/api/configs/{item["id"]}/content', files={"file": ("x.yaml", f"value: {version}\n".encode())})
        assert response.status_code == 200
        assert response.json()["version"] == version
    directory = application.state.store.private / item["id"] / "versions"
    assert {p.name for p in directory.iterdir()} == {f"{n}.yaml" for n in range(3, 9)}
    with application.state.store.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 6
    assert client.get(f'/api/configs/{item["id"]}/versions').status_code == 200
