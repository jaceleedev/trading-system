import pytest

from trading_research.config import Settings


def test_ignores_unrelated_production_database_url(monkeypatch):
    monkeypatch.delenv("TRADING_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://company:secret@production/company")
    settings = Settings.from_env()
    assert settings.database_url.host == "127.0.0.1"
    assert settings.database_url.port == 55432


def test_redacts_password(monkeypatch):
    monkeypatch.setenv("TRADING_DATABASE_URL", "postgresql+psycopg://u:private@localhost/research")
    assert "private" not in Settings.from_env().safe_database_url


def test_rejects_other_database_engines(monkeypatch):
    monkeypatch.setenv("TRADING_DATABASE_URL", "sqlite:///tmp.db")
    with pytest.raises(ValueError, match="postgresql"):
        Settings.from_env()


def test_redacts_query_credentials(monkeypatch):
    monkeypatch.setenv(
        "TRADING_DATABASE_URL", "postgresql+psycopg://localhost/research?password=private"
    )
    assert "private" not in Settings.from_env().safe_database_url
