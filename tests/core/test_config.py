"""Tests for the settings: fixed paths, model names, and the loader that reads the
environment and the .env file."""

import dataclasses
import inspect
import os
from pathlib import Path

import pytest

from app import config
from app.config import Settings, load_settings

ENV_NAMES = (
    "OPENAI_API_KEY",
    "TYPESAFE_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "DECIDER",
    "BUDGET_USD",
)
DEFAULT_HOST = "https://cloud.langfuse.com"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Keep the developer's real environment out of every test."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_paths_hang_off_the_repo_root():
    assert config.ROOT.is_absolute()
    assert (config.ROOT / "pyproject.toml").is_file()
    assert config.DATA_DIR == config.ROOT / "data"
    assert config.RAW_DIR == config.DATA_DIR / "raw"
    assert config.TRUTH_DIR == config.DATA_DIR / "truth"
    assert config.KG_PATH == config.DATA_DIR / "kg.sqlite"
    assert config.CACHE_DIR == config.DATA_DIR / "cache"


def test_model_ids():
    assert config.SOL_MODEL == "gpt-6.1-sol"
    assert config.TERRA_MODEL == "gpt-6.1-terra"
    assert config.DECISIONS_MODEL == "gpt-6-luna"
    assert config.JEV_MODEL == "jev-1.14.0"


def test_env_file_argument_defaults_to_the_repo_dotenv():
    default = inspect.signature(load_settings).parameters["env_file"].default
    assert default == config.ROOT / ".env"


def test_defaults_without_env_file_or_environment(tmp_path):
    settings = load_settings(tmp_path / "missing.env")
    assert settings.openai_api_key is None
    assert settings.typesafe_api_key is None
    assert settings.langfuse_public_key is None
    assert settings.langfuse_secret_key is None
    assert settings.langfuse_host == DEFAULT_HOST
    assert settings.decider == "auto"
    assert settings.budget_usd == 40.0
    assert not settings.has_openai
    assert not settings.has_typesafe
    assert not settings.has_langfuse


def test_values_come_from_the_env_file(tmp_path):
    env = write_env(
        tmp_path,
        "OPENAI_API_KEY=openai-from-file\n"
        "TYPESAFE_API_KEY=typesafe-from-file\n"
        "LANGFUSE_PUBLIC_KEY=public-from-file\n"
        "LANGFUSE_SECRET_KEY=secret-from-file\n"
        "LANGFUSE_HOST=https://langfuse.example.test\n"
        "DECIDER=jev\n"
        "BUDGET_USD=12.5\n",
    )
    settings = load_settings(env)
    assert settings.openai_api_key == "openai-from-file"
    assert settings.typesafe_api_key == "typesafe-from-file"
    assert settings.langfuse_public_key == "public-from-file"
    assert settings.langfuse_secret_key == "secret-from-file"
    assert settings.langfuse_host == "https://langfuse.example.test"
    assert settings.decider == "jev"
    assert settings.budget_usd == 12.5


def test_environment_beats_file(tmp_path, monkeypatch):
    env = write_env(
        tmp_path,
        "OPENAI_API_KEY=from-file\nDECIDER=jev\nBUDGET_USD=5\n",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    monkeypatch.setenv("DECIDER", "decisions")
    settings = load_settings(env)
    assert settings.openai_api_key == "from-env"
    assert settings.decider == "decisions"
    # A value only the file sets still comes through.
    assert settings.budget_usd == 5.0


def test_empty_string_in_file_counts_as_unset(tmp_path):
    env = write_env(
        tmp_path,
        'OPENAI_API_KEY=\nTYPESAFE_API_KEY=""\nLANGFUSE_HOST=\nDECIDER=\nBUDGET_USD=\n',
    )
    settings = load_settings(env)
    assert settings.openai_api_key is None
    assert settings.typesafe_api_key is None
    assert settings.langfuse_host == DEFAULT_HOST
    assert settings.decider == "auto"
    assert settings.budget_usd == 40.0
    assert not settings.has_openai
    assert not settings.has_typesafe


def test_empty_string_in_environment_counts_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("LANGFUSE_HOST", "")
    monkeypatch.setenv("DECIDER", "")
    monkeypatch.setenv("BUDGET_USD", "")
    settings = load_settings(tmp_path / "missing.env")
    assert settings.openai_api_key is None
    assert settings.langfuse_host == DEFAULT_HOST
    assert settings.decider == "auto"
    assert settings.budget_usd == 40.0


def test_empty_environment_value_does_not_hide_the_file_value(tmp_path, monkeypatch):
    env = write_env(tmp_path, "OPENAI_API_KEY=from-file\n")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert load_settings(env).openai_api_key == "from-file"


@pytest.mark.parametrize(
    "raw",
    [
        "key-from-env\n",
        "key-from-env \n",
        "key-from-env\r\n",
        "key-from-env  ",
        " \tkey-from-env\t ",
    ],
)
def test_environment_values_are_stripped_of_surrounding_whitespace(
    tmp_path, monkeypatch, raw
):
    # An exported key can carry a trailing newline or CR. Left on, it would reach a
    # request header, which refuses it and puts the key in the error text.
    monkeypatch.setenv("OPENAI_API_KEY", raw)
    monkeypatch.setenv("TYPESAFE_API_KEY", raw)
    settings = load_settings(tmp_path / "missing.env")
    assert settings.openai_api_key == "key-from-env"
    assert settings.typesafe_api_key == "key-from-env"


def test_every_environment_value_is_stripped_not_only_the_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DECIDER", "jev\n")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.test \r\n")
    settings = load_settings(tmp_path / "missing.env")
    assert settings.decider == "jev"
    assert settings.langfuse_host == "https://langfuse.example.test"


def test_a_whitespace_only_environment_value_counts_as_unset(tmp_path, monkeypatch):
    env = write_env(tmp_path, "OPENAI_API_KEY=from-file\n")
    monkeypatch.setenv("OPENAI_API_KEY", " \n")  # does not hide the file value
    monkeypatch.setenv("TYPESAFE_API_KEY", "\t")  # and with no file value it is unset
    settings = load_settings(env)
    assert settings.openai_api_key == "from-file"
    assert settings.typesafe_api_key is None
    assert not settings.has_typesafe


def test_env_file_syntax(tmp_path):
    env = write_env(
        tmp_path,
        "\n"
        "# a full-line comment\n"
        "\n"
        'OPENAI_API_KEY="double-quoted"\n'
        "TYPESAFE_API_KEY='single-quoted'\n"
        "  LANGFUSE_PUBLIC_KEY  =  spaced out  \n"
        "LANGFUSE_SECRET_KEY=has=equals=inside\n"
        "# LANGFUSE_HOST=https://commented-out.example.test\n"
        "this line has no equals sign\n",
    )
    settings = load_settings(env)
    assert settings.openai_api_key == "double-quoted"
    assert settings.typesafe_api_key == "single-quoted"
    assert settings.langfuse_public_key == "spaced out"
    assert settings.langfuse_secret_key == "has=equals=inside"
    assert settings.langfuse_host == DEFAULT_HOST


def test_only_matching_quotes_are_stripped(tmp_path):
    env = write_env(
        tmp_path,
        "OPENAI_API_KEY=\"mismatched'\n"
        'TYPESAFE_API_KEY=" padded "\n'
        'LANGFUSE_PUBLIC_KEY="\n',
    )
    settings = load_settings(env)
    assert settings.openai_api_key == "\"mismatched'"
    assert settings.typesafe_api_key == " padded "
    assert settings.langfuse_public_key == '"'


def test_windows_line_endings(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"OPENAI_API_KEY=abc\r\nDECIDER=jev\r\n")
    settings = load_settings(path)
    assert settings.openai_api_key == "abc"
    assert settings.decider == "jev"


def test_loading_does_not_touch_the_process_environment(tmp_path):
    load_settings(write_env(tmp_path, "OPENAI_API_KEY=from-file\n"))
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.parametrize("value", ["auto", "jev", "decisions"])
def test_valid_deciders(tmp_path, value):
    assert load_settings(write_env(tmp_path, f"DECIDER={value}\n")).decider == value


@pytest.mark.parametrize("value", ["bogus", "JEV", "openai", "jev,decisions"])
def test_invalid_decider_in_file_raises(tmp_path, value):
    env = write_env(tmp_path, f"DECIDER={value}\n")
    with pytest.raises(ValueError, match="DECIDER"):
        load_settings(env)


def test_invalid_decider_in_environment_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DECIDER", "bogus")
    with pytest.raises(ValueError, match="DECIDER"):
        load_settings(tmp_path / "missing.env")


def test_budget_is_a_float(tmp_path):
    budget = load_settings(write_env(tmp_path, "BUDGET_USD=40\n")).budget_usd
    assert budget == 40.0
    assert isinstance(budget, float)


def test_invalid_budget_raises(tmp_path):
    env = write_env(tmp_path, "BUDGET_USD=lots\n")
    with pytest.raises(ValueError, match="BUDGET_USD"):
        load_settings(env)


@pytest.mark.parametrize(
    ("value", "budget"),
    [
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("-5", None),
        ("0", None),
        ("12.5", 12.5),
    ],
)
def test_budget_must_be_finite_and_above_zero(tmp_path, value, budget):
    env = write_env(tmp_path, f"BUDGET_USD={value}\n")
    if budget is None:
        with pytest.raises(ValueError, match="BUDGET_USD"):
            load_settings(env)
    else:
        assert load_settings(env).budget_usd == budget


def test_repr_shows_no_key_values(tmp_path):
    secrets = (
        "sentinel-openai",
        "sentinel-typesafe",
        "sentinel-public",
        "sentinel-secret",
    )
    env = write_env(
        tmp_path,
        "OPENAI_API_KEY=sentinel-openai\n"
        "TYPESAFE_API_KEY=sentinel-typesafe\n"
        "LANGFUSE_PUBLIC_KEY=sentinel-public\n"
        "LANGFUSE_SECRET_KEY=sentinel-secret\n",
    )
    settings = load_settings(env)
    for text in (repr(settings), str(settings), f"{settings}"):
        for secret in secrets:
            assert secret not in text
    assert "langfuse_host" in repr(settings)


def test_exactly_the_four_secret_fields_are_hidden_from_repr():
    hidden = {f.name for f in dataclasses.fields(Settings) if not f.repr}
    assert hidden == {
        "openai_api_key",
        "typesafe_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
    }


def test_has_properties():
    assert Settings(openai_api_key="key").has_openai
    assert not Settings().has_openai
    assert Settings(typesafe_api_key="key").has_typesafe
    assert not Settings().has_typesafe


def test_langfuse_needs_both_keys():
    assert not Settings(langfuse_public_key="public").has_langfuse
    assert not Settings(langfuse_secret_key="secret").has_langfuse
    both = Settings(langfuse_public_key="public", langfuse_secret_key="secret")
    assert both.has_langfuse


def test_settings_are_frozen():
    settings = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.decider = "jev"
