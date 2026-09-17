import pytest

from ayce.config import ENV_PREFIX, Config, ConfigError, load_env_file


def test_defaults():
    cfg = Config.from_env(env={})
    assert cfg.log_level == "INFO"
    assert cfg.data_dir.name == "data"


def test_env_overrides():
    cfg = Config.from_env(env={f"{ENV_PREFIX}LOG_LEVEL": "debug", f"{ENV_PREFIX}DATA_DIR": "out"})
    assert cfg.log_level == "DEBUG"
    assert cfg.data_dir == type(cfg.data_dir)("out")


def test_invalid_log_level_raises():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Config.from_env(env={f"{ENV_PREFIX}LOG_LEVEL": "VERBOSE"})


def test_empty_data_dir_raises():
    with pytest.raises(ConfigError, match="DATA_DIR"):
        Config.from_env(env={f"{ENV_PREFIX}DATA_DIR": "  "})


def test_require_missing_key_raises():
    cfg = Config.from_env(env={})
    with pytest.raises(ConfigError, match="AYCE_MISSING_KEY"):
        cfg.require("MISSING_KEY", env={})


def test_require_present_key():
    cfg = Config.from_env(env={})
    assert cfg.require("PRESENT", env={f"{ENV_PREFIX}PRESENT": "v"}) == "v"


def test_load_env_file_parses_and_does_not_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nAYCE_A=1\nAYCE_B = \"quoted\"\n\nBAD LINE\n", encoding="utf-8"
    )
    monkeypatch.setenv("AYCE_A", "env-wins")
    applied = load_env_file(env_file)
    import os

    assert os.environ["AYCE_A"] == "env-wins"  # existing env not overridden
    assert os.environ["AYCE_B"] == "quoted"
    assert applied == {"AYCE_B": "quoted"}


def test_load_env_file_missing_is_noop(tmp_path):
    assert load_env_file(tmp_path / "does-not-exist.env") == {}
