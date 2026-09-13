"""Explicit, bounded catalog/workspace CLI surfaces; no implicit global scope."""

from okto_grafx import connect


def test_catalog_inventory_json_and_truncation(cli, tmp_path):
    for name in ("main", "other"):
        with connect(tmp_path / name) as db:
            db.checkpoint()
    args = ("catalogs", str(tmp_path / "main"), "--allow-root", str(tmp_path),
            "--attach", "other=" + str(tmp_path / "other"), "--json")
    result = cli(*args)
    assert result.code == 0, result.text
    assert [item["alias"] for item in result.document["catalogs"]] == ["main", "other"]
    assert all(item["read_only"] for item in result.document["catalogs"])
    assert all(len(item["database_uuid"]) == 32 for item in result.document["catalogs"])
    assert cli(*args, "--limit", "0").document["truncated"]


def test_resolution_is_not_an_open_or_mkdir(cli, tmp_path, monkeypatch):
    import okto_grafx.catalogs as module
    monkeypatch.setattr(module, "connect", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("opened store")))
    (tmp_path / ".git").mkdir()
    args = ("workspace", "resolve", str(tmp_path), "--allow-root", str(tmp_path), "--json")
    result = cli(*args)
    assert result.code == 0, result.text
    assert result.document["workspace"]["source"] == "marker"
    assert result.document["stores_opened"] == 0
    assert not (tmp_path / ".grafx").exists()
    user = str(tmp_path / "user")
    assert cli(*args, "--user-store", user).code != 0
    assert cli(*args, "--user-store", user, "--allow-user-store").code == 0
    assert not (tmp_path / "user").exists()


def test_catalog_cli_refuses_missing_roots_creation_and_escape(cli, tmp_path):
    missing = tmp_path / "missing"
    assert cli("catalogs", str(missing), "--json").code != 0
    assert cli("catalogs", str(missing), "--allow-root", str(tmp_path), "--create", "--json").code != 0
    assert not missing.exists()
    assert cli("workspace", "resolve", str(tmp_path.parent), "--allow-root", str(tmp_path), "--allow-cwd", "--json").code != 0
