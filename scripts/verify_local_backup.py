"""Back up and restore-check only this project's isolated local PostgreSQL.

No source restore, source DROP, shell command, remote target, or daemon exists in
this script. The backup and its verification record remain in var/backups.
"""

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from trading_research import models
from trading_research.config import Settings
from trading_research.errors import DataError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = (
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/18/bin"
)
_INSPECT_FORMAT = (
    '{"id":{{json .Id}},"running":{{json .State.Running}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"image":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}'
)


def validate_local_url(url):
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 55432
        or url.database != "trading"
        or url.username != "trading"
        or not url.password
        or url.query
    ):
        raise DataError("Backup is restricted to the dedicated 127.0.0.1:55432/trading database")


def validate_docker_endpoint(endpoint: str):
    if not isinstance(endpoint, str):
        raise DataError("Docker endpoint must be a local Unix socket")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or parsed.query
        or parsed.fragment
    ):
        raise DataError("Docker endpoint must be a local Unix socket")


def validate_container(details: dict):
    if (
        type(details) is not dict
        or set(details) != {"id", "running", "project", "service", "image", "ports"}
        or not isinstance(details["id"], str)
        or re.fullmatch(r"[0-9a-f]{64}", details["id"]) is None
        or details["running"] is not True
        or details["project"] != "trading-research"
        or details["service"] != "postgres"
        or details["image"] != "postgres:18.6"
        or details["ports"] != {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
    ):
        raise DataError("Container is not the dedicated local trading-research PostgreSQL service")


def validate_restore_name(name: str):
    if (
        not isinstance(name, str)
        or re.fullmatch(r"trading_restore_check_[0-9a-f]{32}", name) is None
    ):
        raise DataError("Restore database name is outside the ephemeral run namespace")


def _command(argv, *, stdin=None, stdout=subprocess.PIPE):
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_CONFIG_HOME")
        if key in os.environ
    }
    try:
        result = subprocess.run(
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=300,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        raise DataError("Local backup command failed; command output omitted") from None
    if result.returncode != 0:
        raise DataError("Local backup command failed; command output omitted")
    return result.stdout


@dataclass(frozen=True)
class LocalDocker:
    context: str
    container_id: str

    def execute(self, program, arguments, *, stdin=None, stdout=subprocess.PIPE):
        return _command(
            [
                "docker",
                "--context",
                self.context,
                "exec",
                "-i",
                self.container_id,
                "env",
                "-i",
                f"PATH={_ENV_PATH}",
                program,
                *arguments,
            ],
            stdin=stdin,
            stdout=stdout,
        )


def guarded_target():
    # Refuse inherited libpq routing/options rather than relying on their precedence.
    if os.environ.get("DOCKER_HOST") or any(key.startswith("PG") for key in os.environ):
        raise DataError("Clear Docker host and libpq PG environment overrides before local backup")
    try:
        url = Settings.from_env().database_url
        validate_local_url(url)
        context = _command(["docker", "context", "show"]).decode().strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", context) is None:
            raise DataError("Docker context name is invalid")
        endpoint = json.loads(
            _command(
                [
                    "docker",
                    "context",
                    "inspect",
                    context,
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ]
            )
        )
        validate_docker_endpoint(endpoint)
        container_id = (
            _command(
                [
                    "docker",
                    "--context",
                    context,
                    "compose",
                    "--project-name",
                    "trading-research",
                    "-f",
                    str(PROJECT_ROOT / "compose.yaml"),
                    "ps",
                    "-q",
                    "postgres",
                ]
            )
            .decode()
            .strip()
        )
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise DataError("Exactly one dedicated local database container is required")
        details = json.loads(
            _command(
                [
                    "docker",
                    "--context",
                    context,
                    "inspect",
                    "--format",
                    _INSPECT_FORMAT,
                    container_id,
                ]
            )
        )
        validate_container(details)
        return url, LocalDocker(context, container_id)
    except DataError:
        raise
    except Exception:
        raise DataError("Local database safety checks failed; connection details omitted") from None


def connect_local(url, database="trading"):
    validate_local_url(url)
    if database != "trading":
        validate_restore_name(database)
    return psycopg.connect(
        host="127.0.0.1",
        hostaddr="127.0.0.1",
        port=55432,
        dbname=database,
        user="trading",
        password=url.password,
        connect_timeout=5,
        autocommit=True,
        sslmode="disable",
        gssencmode="disable",
        passfile=os.devnull,
        application_name="trading_research_local_backup",
        options="-c timezone=UTC -c datestyle=ISO,YMD -c extra_float_digits=3 "
        "-c bytea_output=hex -c search_path=pg_catalog,public "
        "-c statement_timeout=300000 -c lock_timeout=5000",
    )


def verify_same_server(connection, docker):
    database, user, version, identifier = connection.execute(
        "SELECT current_database(), current_user, current_setting('server_version_num'), "
        "system_identifier FROM pg_control_system()"
    ).fetchone()
    container_identifier = (
        docker.execute(
            "psql",
            [
                "-X",
                "--no-password",
                "--host=/var/run/postgresql",
                "--port=5432",
                "--username=trading",
                "--dbname=trading",
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "SELECT system_identifier FROM pg_control_system()",
            ],
        )
        .decode()
        .strip()
    )
    if (
        database != "trading"
        or user != "trading"
        or not 180000 <= int(version) < 190000
        or str(identifier) != container_identifier
    ):
        raise DataError(
            "Published local connection and verified container do not identify one server"
        )


class RestoreDatabase:
    def __init__(self, connection, name: str, marker: str):
        validate_restore_name(name)
        self.connection, self.name, self.marker = connection, name, marker
        self.created, self.oid = False, None

    def create(self):
        if self.created:
            raise DataError("This run already created its restore database")
        validate_restore_name(self.name)
        self.connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(self.name))
        )
        # A duplicate-name or ambiguous CREATE failure must never authorize DROP.
        self.created = True
        self.oid = self.connection.execute(
            "SELECT oid FROM pg_database WHERE datname = %s", (self.name,)
        ).fetchone()[0]
        self.connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(self.name), sql.Literal(self.marker)
            )
        )

    def drop(self):
        if not self.created:
            return
        validate_restore_name(self.name)
        observed = self.connection.execute(
            "SELECT oid, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
            (self.name,),
        ).fetchone()
        if self.oid is None or observed != (self.oid, self.marker):
            raise DataError("Restore database ownership changed; cleanup refused")
        self.connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(self.name)))
        self.created = False


def exclusive_file(path: Path):
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
    finally:
        os.close(directory)
    return os.fdopen(descriptor, "w+b")


def hash_rows(rows):
    digest, count = hashlib.sha256(), 0
    for (text,) in rows:
        raw = text.encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def database_evidence(connection):
    schemas = connection.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
        "AND nspname <> 'information_schema' ORDER BY nspname"
    ).fetchall()
    if schemas != [("public",)]:
        raise DataError("Database contains a schema outside this research workspace")
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    expected = set(models.Base.metadata.tables) | {"alembic_version"}
    if {name for (name,) in tables} != expected:
        raise DataError("Local database tables do not match the application's migrated schema")
    evidence = {}
    for (name,) in tables:
        with connection.cursor(name="backup_hash_" + uuid.uuid4().hex) as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT to_jsonb(t)::text FROM {}.{} AS t "
                    'ORDER BY (to_jsonb(t)::text) COLLATE "C"'
                ).format(sql.Identifier("public"), sql.Identifier(name))
            )
            evidence[name] = hash_rows(cursor)
        columns = connection.execute(
            "SELECT column_name, data_type, udt_schema, udt_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (name,),
        ).fetchall()
        constraints = connection.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass ORDER BY conname",
            ("public." + name,),
        ).fetchall()
        indexes = connection.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname",
            (name,),
        ).fetchall()
        evidence[name]["schema_sha256"] = hashlib.sha256(
            json.dumps(
                [columns, constraints, indexes], ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
    return {
        "tables": evidence,
        "alembic_revisions": [
            row[0]
            for row in connection.execute(
                "SELECT version_num FROM public.alembic_version ORDER BY version_num"
            ).fetchall()
        ],
    }


def _read_transaction(connection):
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def run_backup():
    url, docker = guarded_target()
    run_id = uuid.uuid4().hex
    restore_name = "trading_restore_check_" + run_id
    backup_root = PROJECT_ROOT / "var" / "backups"
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"trading-{stamp}-{run_id}"
    dump_path, report_path = backup_root / (stem + ".dump"), backup_root / (stem + ".json")
    report = {
        "kind": "isolated_local_backup_verification",
        "status": "failed",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "source": "127.0.0.1:55432/trading",
        "docker_context": docker.context,
        "container_id": docker.container_id,
        "dump_path": str(dump_path),
        "verification_path": str(report_path),
        "restore_database": restore_name,
        "dump_completed": False,
        "restore_completed": False,
        "snapshot_comparison_passed": False,
        "cleanup": "not_created",
        "hash_method": "length-prefixed UTF-8 PostgreSQL JSONB rows sorted with C collation; UTC",
        "scope": "application public tables and Alembic revision; not cluster roles or tablespaces",
    }
    with exclusive_file(dump_path) as dump, exclusive_file(report_path) as receipt:
        try:
            with connect_local(url) as source:
                verify_same_server(source, docker)
                temporary = RestoreDatabase(source, restore_name, "trading-local-backup:" + run_id)
                try:
                    with source.transaction():
                        _read_transaction(source)
                        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
                        if re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot) is None:
                            raise DataError("Unexpected exported snapshot identifier")
                        report["source_snapshot"] = database_evidence(source)
                        docker.execute(
                            "pg_dump",
                            [
                                "--format=custom",
                                "--no-password",
                                "--host=/var/run/postgresql",
                                "--port=5432",
                                "--username=trading",
                                "--dbname=trading",
                                "--snapshot=" + snapshot,
                            ],
                            stdout=dump,
                        )
                        dump.flush()
                        os.fsync(dump.fileno())
                        dump.seek(0)
                        if dump.read(5) != b"PGDMP":
                            raise DataError("Backup is not a PostgreSQL custom-format archive")
                        dump.seek(0)
                        report["dump_sha256"] = hashlib.file_digest(dump, "sha256").hexdigest()
                        report["dump_bytes"] = dump.seek(0, os.SEEK_END)
                        report["dump_completed"] = True
                    temporary.create()
                    report["restore_database_oid"] = temporary.oid
                    report["cleanup"] = "pending"
                    dump.seek(0)
                    docker.execute(
                        "pg_restore",
                        [
                            "--exit-on-error",
                            "--single-transaction",
                            "--no-owner",
                            "--no-privileges",
                            "--no-password",
                            "--host=/var/run/postgresql",
                            "--port=5432",
                            "--username=trading",
                            "--dbname=" + restore_name,
                        ],
                        stdin=dump,
                        stdout=subprocess.DEVNULL,
                    )
                    report["restore_completed"] = True
                    with connect_local(url, restore_name) as restored, restored.transaction():
                        _read_transaction(restored)
                        report["restored_snapshot"] = database_evidence(restored)
                    if report["source_snapshot"] != report["restored_snapshot"]:
                        raise DataError("Restored rows, schema, or migration revision do not match")
                    report["snapshot_comparison_passed"] = True
                    report["status"] = "passed"
                finally:
                    if temporary.created:
                        try:
                            temporary.drop()
                            report["cleanup"] = "dropped_owned_database"
                        except Exception:
                            report["status"] = "failed"
                            report["cleanup"] = "failed_or_ownership_changed"
        except Exception:
            report["status"] = "failed"
            report["error"] = (
                "Local backup verification failed; database and command details omitted"
            )
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            receipt.write(json.dumps(report, indent=2, sort_keys=True).encode())
            receipt.flush()
            os.fsync(receipt.fileno())
    return report


def main():
    try:
        report = run_backup()
    except Exception:
        print("Local backup safety checks or artifact creation failed; details omitted")
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "dump_path",
                    "verification_path",
                    "snapshot_comparison_passed",
                    "cleanup",
                )
            },
            indent=2,
        )
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
