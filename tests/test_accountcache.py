import json

from ajl import accountcache


def test_cache_file_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("AJL_ACCOUNT_CACHE_FILE", raising=False)
    assert accountcache.cache_file().endswith("ajl/accounts.json")
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", "/tmp/custom-accounts.json")
    assert accountcache.cache_file() == "/tmp/custom-accounts.json"


def test_get_miss_on_empty_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(tmp_path / "accounts.json"))
    assert accountcache.get("prod") is None


def test_put_then_get_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(tmp_path / "sub" / "accounts.json"))
    accountcache.put("prod", "123456789012")
    assert accountcache.get("prod") == "123456789012"
    # a different profile in the same file is unaffected
    assert accountcache.get("stage") is None


def test_get_ignores_expired_entry(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(path))
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_TTL", "1")
    accountcache.put("prod", "123456789012")
    assert accountcache.get("prod") == "123456789012"
    entry = json.loads(path.read_text())
    entry["prod"]["ts"] -= 10  # simulate the entry aging past the 1s TTL
    path.write_text(json.dumps(entry))
    assert accountcache.get("prod") is None


def test_get_and_put_ignore_falsy_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(tmp_path / "accounts.json"))
    accountcache.put(None, "123456789012")
    accountcache.put("", "123456789012")
    assert accountcache.get(None) is None
    assert accountcache.get("") is None
    assert not (tmp_path / "accounts.json").exists()


def test_put_ignores_falsy_account(tmp_path, monkeypatch):
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(tmp_path / "accounts.json"))
    accountcache.put("prod", "")
    assert not (tmp_path / "accounts.json").exists()
