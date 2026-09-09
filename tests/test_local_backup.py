import copy
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest
from psycopg.errors import DuplicateDatabase
from sqlalchemy.engine import URL

from trading_research.errors import DataError


@pytest.fixture(scope="module")
def backup():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_local_backup.py"
    spec = importlib.util.spec_from_file_location("verify_local_backup_tests", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_url():
    return URL.create(
        "postgresql+psycopg",
        username="trading",
        password="local-password",
        host="127.0.0.1",
        port=55432,
        database="trading",
    )


@pytest.fixture
def container_details():
    return {
        "id": "a" * 64,
        "running": True,
        "project": "trading-research",
        "service": "postgres",
        "image": "postgres:18.6",
        "ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
    }


def test_local_url_accepts_only_the_designated_source(backup, local_url):
    backup.validate_local_url(local_url)


@pytest.mark.parametrize(
    "changes",
    [
        {"drivername": "postgresql"},
        {"drivername": "postgresql+psycopg2"},
        {"host": "localhost"},
        {"host": "192.0.2.1"},
        {"host": "::1"},
        {"port": 5432},
        {"database": "postgres"},
        {"database": "trading_restore_check_" + "a" * 32},
        {"username": "postgres"},
        {"password": ""},
        {"query": {"host": "192.0.2.1"}},
        {"query": {"service": "production"}},
        {"query": {"sslmode": "disable"}},
    ],
)
def test_local_url_rejects_other_targets_and_connection_overrides(backup, local_url, changes):
    with pytest.raises(DataError):
        backup.validate_local_url(local_url.set(**changes))


@pytest.mark.parametrize("missing", ["host", "port", "database", "username", "password"])
def test_local_url_rejects_missing_connection_fields(backup, local_url, missing):
    fields = local_url._asdict()
    fields[missing] = None
    with pytest.raises(DataError):
        backup.validate_local_url(URL.create(**fields))


@pytest.mark.parametrize("endpoint", ["unix:///var/run/docker.sock", "unix:///tmp/docker.sock"])
def test_docker_endpoint_accepts_absolute_unix_sockets(backup, endpoint):
    backup.validate_docker_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "tcp://127.0.0.1:2375",
        "tcp://192.0.2.1:2376",
        "ssh://localhost",
        "unix://localhost/var/run/docker.sock",
        "unix://relative.sock",
        "unix:relative.sock",
        "/var/run/docker.sock",
    ],
)
def test_docker_endpoint_rejects_remote_and_ambiguous_endpoints(backup, endpoint):
    with pytest.raises(DataError):
        backup.validate_docker_endpoint(endpoint)


def test_container_accepts_running_isolated_postgres(backup, container_details):
    backup.validate_container(container_details)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "a" * 12),
        ("id", "A" * 64),
        ("running", False),
        ("running", "true"),
        ("project", "other-project"),
        ("service", "other-postgres"),
        ("image", "postgres:latest"),
        ("ports", {}),
        ("ports", {"5432/tcp": None}),
        ("ports", {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "55432"}]}),
        ("ports", {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}),
        (
            "ports",
            {
                "5432/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                    {"HostIp": "0.0.0.0", "HostPort": "55432"},
                ]
            },
        ),
    ],
)
def test_container_rejects_identity_or_port_mismatch(backup, container_details, field, value):
    details = copy.deepcopy(container_details)
    details[field] = value
    with pytest.raises(DataError):
        backup.validate_container(details)


@pytest.mark.parametrize("missing", ["id", "running", "project", "service", "image", "ports"])
def test_container_rejects_incomplete_inspection(backup, container_details, missing):
    del container_details[missing]
    with pytest.raises(DataError):
        backup.validate_container(container_details)


def test_restore_name_accepts_generated_database_name(backup):
    backup.validate_restore_name("trading_restore_check_" + "0123456789abcdef" * 2)


@pytest.mark.parametrize(
    "name",
    [
        "trading",
        "postgres",
        "trading_restore_check_",
        "trading_restore_check_" + "a" * 31,
        "trading_restore_check_" + "a" * 33,
        "trading_restore_check_" + "A" * 32,
        "trading_restore_check_" + "g" * 32,
        "trading_restore_check_" + "a" * 32 + "; DROP DATABASE trading;",
    ],
)
def test_restore_name_rejects_source_and_unsafe_names(backup, name):
    with pytest.raises(DataError):
        backup.validate_restore_name(name)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, marker, create_error=None):
        self.marker = marker
        self.create_error = create_error
        self.calls = []
        self.ownership = (1234, marker)

    def execute(self, statement, parameters=None):
        query = statement.as_string() if hasattr(statement, "as_string") else str(statement)
        query = " ".join(query.split())
        self.calls.append((query, parameters))
        if query.startswith("CREATE DATABASE") and self.create_error is not None:
            raise self.create_error
        if query.startswith("SELECT oid,"):
            return FakeResult(self.ownership)
        if query.startswith("SELECT oid"):
            return FakeResult((1234,))
        return FakeResult(None)


@pytest.fixture
def restore_database(backup):
    name = "trading_restore_check_" + "a" * 32
    marker = "local-backup-verification:" + "b" * 32
    connection = FakeConnection(marker)
    return connection, backup.RestoreDatabase(connection, name, marker)


def test_restore_database_does_not_drop_before_successful_creation(restore_database):
    connection, database = restore_database
    assert database.created is False
    database.drop()
    assert connection.calls == []


def test_restore_database_uses_empty_template_and_verifies_ownership_before_drop(restore_database):
    connection, database = restore_database
    database.create()
    assert database.created is True
    assert database.oid == 1234
    assert connection.calls[0][0] == f'CREATE DATABASE "{database.name}" TEMPLATE template0'
    assert any(
        query.startswith("COMMENT ON DATABASE") and database.marker in query
        for query, _ in connection.calls
    )

    connection.calls.clear()
    database.drop()
    assert connection.calls[0][0].startswith("SELECT oid,")
    assert "shobj_description(oid, 'pg_database')" in connection.calls[0][0]
    assert " obj_description(" not in connection.calls[0][0]
    assert connection.calls[0][1] == (database.name,)
    assert connection.calls[-1][0] == f'DROP DATABASE "{database.name}"'
    assert database.created is False

    connection.calls.clear()
    database.drop()
    assert connection.calls == []


@pytest.mark.parametrize(
    "error", [DuplicateDatabase("already exists"), RuntimeError("create failed")]
)
def test_restore_database_never_drops_after_create_failure(backup, error):
    name = "trading_restore_check_" + "a" * 32
    connection = FakeConnection("owned-marker", create_error=error)
    database = backup.RestoreDatabase(connection, name, connection.marker)
    with pytest.raises(type(error)):
        database.create()
    assert database.created is False

    connection.calls.clear()
    database.drop()
    assert connection.calls == []


@pytest.mark.parametrize("mismatch", ["oid", "marker", "missing"])
def test_restore_database_refuses_cleanup_without_matching_oid_and_marker(
    restore_database, mismatch
):
    connection, database = restore_database
    database.create()
    connection.ownership = {
        "oid": (9999, database.marker),
        "marker": (1234, "someone-else"),
        "missing": None,
    }[mismatch]
    connection.calls.clear()
    with pytest.raises(DataError):
        database.drop()
    assert database.created is True
    assert not any(query.startswith("DROP DATABASE") for query, _ in connection.calls)


def test_restore_database_cannot_create_the_source(backup):
    connection = FakeConnection("owned-marker")
    with pytest.raises(DataError):
        backup.RestoreDatabase(connection, "trading", connection.marker).create()
    assert connection.calls == []


def test_restore_database_cannot_drop_the_source_even_if_name_changes(restore_database):
    connection, database = restore_database
    database.create()
    database.name = "trading"
    connection.calls.clear()
    with pytest.raises(DataError):
        database.drop()
    assert not any(query.startswith("DROP DATABASE") for query, _ in connection.calls)


def test_exclusive_backup_file_is_private_and_writable(backup, tmp_path):
    path = tmp_path / "backup.dump"
    with backup.exclusive_file(path) as stream:
        stream.write(b"backup bytes")
    assert path.read_bytes() == b"backup bytes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_exclusive_backup_file_never_overwrites_existing_file(backup, tmp_path):
    path = tmp_path / "backup.dump"
    path.write_bytes(b"existing backup")
    with pytest.raises((OSError, DataError)):
        with backup.exclusive_file(path):
            pass
    assert path.read_bytes() == b"existing backup"


def test_exclusive_backup_file_rejects_destination_symlink(backup, tmp_path):
    target = tmp_path / "existing.dump"
    target.write_bytes(b"existing backup")
    path = tmp_path / "backup.dump"
    path.symlink_to(target)
    with pytest.raises((OSError, DataError)):
        with backup.exclusive_file(path):
            pass
    assert path.is_symlink()
    assert target.read_bytes() == b"existing backup"


def test_exclusive_backup_file_rejects_parent_symlink(backup, tmp_path):
    target = tmp_path / "real-output"
    target.mkdir()
    parent = tmp_path / "linked-output"
    parent.symlink_to(target, target_is_directory=True)
    with pytest.raises((OSError, DataError)):
        with backup.exclusive_file(parent / "backup.dump"):
            pass
    assert not (target / "backup.dump").exists()


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "DOCKER_HOST"])
def test_guarded_target_rejects_routing_overrides_before_external_calls(
    backup, monkeypatch, variable
):
    for key in os.environ:
        if key.startswith("PG") or key == "DOCKER_HOST":
            monkeypatch.delenv(key)
    secret = "private-routing-override-do-not-display"
    monkeypatch.setenv(variable, secret)

    def unexpected_external_call(*args, **kwargs):
        pytest.fail("Routing override must be rejected before subprocess or network calls")

    monkeypatch.setattr(backup, "_command", unexpected_external_call)
    monkeypatch.setattr(backup.psycopg, "connect", unexpected_external_call)
    with pytest.raises(DataError) as error:
        backup.guarded_target()
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([('{"id":1,"value":"alpha"}',)], [('{"id":1,"value":"beta"}',)]),
        ([("ab",), ("c",)], [("a",), ("bc",)]),
        ([("a\nb",), ("c",)], [("a",), ("b\nc",)]),
    ],
)
def test_hash_rows_preserves_all_columns_and_row_boundaries(backup, first, second):
    assert backup.hash_rows(first)["sha256"] != backup.hash_rows(second)["sha256"]


def test_hash_rows_consumes_ordered_rows_and_counts_duplicates(backup):
    rows = [("first",), ("second",), ("second",)]
    result = backup.hash_rows(iter(rows))
    assert result["rows"] == 3
    assert result == backup.hash_rows(rows)
    assert result["sha256"] != backup.hash_rows(reversed(rows))["sha256"]
    assert result["sha256"] != backup.hash_rows(rows[:2])["sha256"]
    assert backup.hash_rows(iter(()))["rows"] == 0
