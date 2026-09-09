import csv
from datetime import UTC, datetime

import pytest

from trading_research.data import DataError, decimal_value, load_bundle, timestamp
from trading_research.demo import generate_demo


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    return generate_demo(tmp_path_factory.mktemp("demo"))


def test_demo_provenance_and_determinism(demo_dir, tmp_path):
    first = load_bundle(demo_dir)
    second = load_bundle(generate_demo(tmp_path / "copy"))
    assert first.sha256 == second.sha256
    assert first.manifest["kind"] == "synthetic"
    assert "not market evidence" in first.evidence_warnings()[0]
    assert len(first.instruments) == 14
    assert all(b.available_at >= b.session_close_at for b in first.bars)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "0.00000000001", "1e18"])
def test_invalid_prices_rejected(value):
    with pytest.raises(DataError):
        decimal_value(value)


def test_requires_aware_timestamp():
    with pytest.raises(DataError, match="offset"):
        timestamp("2025-01-02T16:00:00")
    assert timestamp("2025-01-02T09:00:00+09:00") == datetime(2025, 1, 2, tzinfo=UTC)


def mutate_csv(path, change):
    with path.open() as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_unfinished_bar_rejected(tmp_path):
    directory = generate_demo(tmp_path / "bad")
    mutate_csv(directory / "bars.csv", lambda rows: rows[0].update(is_final="false"))
    with pytest.raises(DataError, match="final"):
        load_bundle(directory)


def test_same_day_lookahead_rejected(tmp_path):
    directory = generate_demo(tmp_path / "bad")
    mutate_csv(
        directory / "bars.csv",
        lambda rows: rows[0].update(available_at="2023-01-02T00:00:00+00:00"),
    )
    with pytest.raises(DataError, match="before"):
        load_bundle(directory)


def test_duplicate_bars_rejected(tmp_path):
    directory = generate_demo(tmp_path / "bad")
    mutate_csv(directory / "bars.csv", lambda rows: rows.append(rows[0]))
    with pytest.raises(DataError, match="unique"):
        load_bundle(directory)


def test_demo_does_not_overwrite(tmp_path):
    generate_demo(tmp_path)
    with pytest.raises(DataError, match="empty"):
        generate_demo(tmp_path)


def test_bundle_reads_each_file_once(demo_dir, monkeypatch):
    from pathlib import Path

    original = Path.read_bytes
    calls = {}

    def read_once(path):
        calls[path] = calls.get(path, 0) + 1
        assert calls[path] == 1, "Parsing and hashing must use the same immutable byte snapshot"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    load_bundle(demo_dir)
    assert len(calls) == 4
