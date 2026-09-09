from traceback import format_exception

import pytest
from sqlalchemy.engine import make_url

from trading_research.config import LOCAL_DATABASE_URL, Settings

COMPONENTS = {
    "TRADING_DATABASE_HOST": "127.0.0.1",
    "TRADING_DATABASE_PORT": "55432",
    "TRADING_DATABASE_NAME": "research",
    "TRADING_DATABASE_USER": "fixture-user",
    "TRADING_DATABASE_PASSWORD": "fixture-password",
}


@pytest.fixture(autouse=True)
def isolate_database_environment(monkeypatch):
    for name in ("TRADING_DATABASE_URL", "DATABASE_URL", *COMPONENTS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def component_environment(monkeypatch):
    for name, value in COMPONENTS.items():
        monkeypatch.setenv(name, value)


def test_default_is_the_exact_existing_local_database_url():
    assert Settings.from_env().database_url == make_url(LOCAL_DATABASE_URL)


def test_ignores_unrelated_production_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://company:secret@production/company")
    settings = Settings.from_env()
    assert settings.database_url == make_url(LOCAL_DATABASE_URL)
    assert settings.database_url.host == "127.0.0.1"
    assert settings.database_url.port == 55432


def test_redacts_password(monkeypatch):
    monkeypatch.setenv("TRADING_DATABASE_URL", "postgresql+psycopg://u:private@localhost/research")
    safe = Settings.from_env().safe_database_url
    assert safe == "postgresql+psycopg://localhost/research"
    assert "private" not in safe


def test_rejects_other_database_engines(monkeypatch):
    monkeypatch.setenv("TRADING_DATABASE_URL", "sqlite:///tmp.db")
    with pytest.raises(ValueError, match="postgresql"):
        Settings.from_env()


def test_redacts_query_credentials(monkeypatch):
    monkeypatch.setenv(
        "TRADING_DATABASE_URL", "postgresql+psycopg://localhost/research?password=private"
    )
    safe = Settings.from_env().safe_database_url
    assert "private" not in safe
    assert make_url(safe).query == {}


def test_component_settings_build_a_postgresql_url(component_environment):
    url = Settings.from_env().database_url
    assert url.drivername == "postgresql+psycopg"
    assert url.host == COMPONENTS["TRADING_DATABASE_HOST"]
    assert url.port == 55432
    assert url.database == COMPONENTS["TRADING_DATABASE_NAME"]
    assert url.username == COMPONENTS["TRADING_DATABASE_USER"]
    assert url.password == COMPONENTS["TRADING_DATABASE_PASSWORD"]


def test_component_settings_ignore_unrelated_database_url(monkeypatch, component_environment):
    monkeypatch.setenv("DATABASE_URL", "postgresql://company:secret@production/company")
    url = Settings.from_env().database_url
    assert url.host == COMPONENTS["TRADING_DATABASE_HOST"]
    assert url.database == "research"
    assert url.username == "fixture-user"


@pytest.mark.parametrize("components", ["partial", "empty", "bad-port", "complete"])
def test_explicit_url_precedes_component_settings(monkeypatch, components):
    if components == "partial":
        monkeypatch.setenv("TRADING_DATABASE_PASSWORD", "unused-fixture-password")
    else:
        for name, value in COMPONENTS.items():
            monkeypatch.setenv(name, "" if components == "empty" else value)
        if components == "bad-port":
            monkeypatch.setenv("TRADING_DATABASE_PORT", "unused-invalid-port")
    expected = "postgresql+psycopg://url-user:url-password@localhost:55433/explicit"
    monkeypatch.setenv("TRADING_DATABASE_URL", expected)
    assert Settings.from_env().database_url == make_url(expected)


@pytest.mark.parametrize("with_components", [False, True])
def test_explicit_empty_url_is_an_error_instead_of_a_fallback(monkeypatch, with_components):
    if with_components:
        for name, value in COMPONENTS.items():
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("TRADING_DATABASE_URL", "")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("missing", COMPONENTS)
def test_partial_component_settings_never_fall_back(monkeypatch, component_environment, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError) as caught:
        Settings.from_env()
    assert COMPONENTS["TRADING_DATABASE_PASSWORD"] not in str(caught.value)


@pytest.mark.parametrize("name", COMPONENTS)
def test_one_present_component_requires_all_components(monkeypatch, name):
    monkeypatch.setenv(name, COMPONENTS[name])
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("name", COMPONENTS)
def test_one_empty_component_is_still_explicit_configuration(monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize("empty", COMPONENTS)
def test_empty_component_is_rejected(monkeypatch, component_environment, empty):
    monkeypatch.setenv(empty, "")
    with pytest.raises(ValueError) as caught:
        Settings.from_env()
    assert COMPONENTS["TRADING_DATABASE_PASSWORD"] not in str(caught.value)


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("65535", 65535), ("0001", 1)])
def test_ascii_port_boundaries_are_accepted(monkeypatch, component_environment, value, expected):
    monkeypatch.setenv("TRADING_DATABASE_PORT", value)
    assert Settings.from_env().database_url.port == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero"),
        pytest.param("65536", id="above-maximum"),
        pytest.param("-1", id="negative"),
        pytest.param("+5432", id="positive-sign"),
        pytest.param(" 5432", id="leading-space"),
        pytest.param("5432 ", id="trailing-space"),
        pytest.param("54 32", id="interior-space"),
        pytest.param("5432\n", id="newline"),
        pytest.param("\t5432", id="tab"),
        pytest.param("54_32", id="separator"),
        pytest.param("5432.0", id="decimal"),
        pytest.param("5e3", id="exponent"),
        pytest.param("0x1538", id="hexadecimal"),
        pytest.param("５４３２", id="full-width-digits"),
        pytest.param("٥٤٣٢", id="arabic-indic-digits"),
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("fixture-secret-in-port", id="secret-in-invalid-port"),
    ],
)
def test_invalid_port_has_a_generic_error(monkeypatch, component_environment, value):
    monkeypatch.setenv("TRADING_DATABASE_PORT", value)
    with pytest.raises(ValueError) as caught:
        Settings.from_env()
    message = str(caught.value)
    assert "fixture-secret-in-port" not in message
    assert COMPONENTS["TRADING_DATABASE_PASSWORD"] not in message
    assert COMPONENTS["TRADING_DATABASE_USER"] not in message
    assert "fixture-secret-in-port" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize("port", [1, 65535])
def test_explicit_url_port_boundaries_are_accepted(monkeypatch, port):
    monkeypatch.setenv("TRADING_DATABASE_URL", f"postgresql+psycopg://localhost:{port}/research")
    assert Settings.from_env().database_url.port == port


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_explicit_url_rejects_out_of_range_ports_without_echoing_auth(monkeypatch, port):
    monkeypatch.setenv(
        "TRADING_DATABASE_URL",
        f"postgresql+psycopg://hidden-user:hidden-password@localhost:{port}/research",
    )
    with pytest.raises(ValueError) as caught:
        Settings.from_env()
    assert "hidden-user" not in str(caught.value)
    assert "hidden-password" not in str(caught.value)


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("a:b@c/d?e#f%g[h]+i&j=k;'\"", id="url-punctuation"),
        pytest.param("비밀번호-秘密-🔐", id="unicode"),
        pytest.param("  fixture password\t ", id="preserved-whitespace"),
        pytest.param("literal%40and%2Fand%25", id="literal-percent-encoding"),
    ],
)
def test_component_password_survives_exact_url_round_trip(
    monkeypatch, component_environment, password
):
    monkeypatch.setenv("TRADING_DATABASE_PASSWORD", password)
    url = Settings.from_env().database_url
    assert url.password == password
    assert make_url(url.render_as_string(hide_password=False)).password == password
    assert url.host == "127.0.0.1"
    assert url.database == "research"


def test_safe_database_url_removes_all_authentication_and_query_fields(monkeypatch):
    monkeypatch.setenv(
        "TRADING_DATABASE_URL",
        "postgresql+psycopg://hidden-user:hidden-password@localhost:55433/research"
        "?password=hidden-query-secret&sslmode=require&options=hidden-options",
    )
    settings = Settings.from_env()
    assert settings.database_url.username == "hidden-user"
    assert settings.database_url.password == "hidden-password"
    assert settings.database_url.query
    assert settings.safe_database_url == "postgresql+psycopg://localhost:55433/research"
    safe = make_url(settings.safe_database_url)
    assert safe.username is None
    assert safe.password is None
    assert safe.query == {}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("malformed-url-hidden-password", id="malformed-url"),
        pytest.param(
            "postgresql+psycopg://hidden-user:hidden-password@localhost:hidden-port/research",
            id="invalid-url-port",
        ),
        pytest.param(
            "mysql://hidden-user:hidden-password@localhost/research?token=hidden-query-secret",
            id="unsupported-engine",
        ),
        pytest.param(
            "postgresql+psycopg://hidden-user:hidden-password@localhost?token=hidden-query-secret",
            id="missing-database",
        ),
        pytest.param(
            "postgresql+psycopg://hidden-user:hidden-password@localhost/?token=hidden-query-secret",
            id="empty-database",
        ),
    ],
)
def test_invalid_url_errors_do_not_echo_credentials(monkeypatch, value):
    monkeypatch.setenv("TRADING_DATABASE_URL", value)
    with pytest.raises(ValueError) as caught:
        Settings.from_env()
    message = "".join(format_exception(caught.value))
    for secret in ("hidden-user", "hidden-password", "hidden-port", "hidden-query-secret"):
        assert secret not in message
    assert value not in message
