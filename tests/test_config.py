from pathlib import Path

from cde_exporter.config import Settings


def test_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.input_dir == Path("data/input")
    assert settings.output_dir == Path("data/output")
    assert settings.microsoft_client_id is None


def test_settings_read_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("CDE_ENV", "test")
    monkeypatch.setenv("CDE_LOG_LEVEL", "debug")
    monkeypatch.setenv("CDE_INPUT_DIR", "incoming")
    monkeypatch.setenv("SHAREPOINT_SITE_ID", "")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"
    assert settings.input_dir == Path("incoming")
    assert settings.sharepoint_site_id is None
