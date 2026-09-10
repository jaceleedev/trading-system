import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from trading_research.errors import DataError
from trading_research.private_store import get_object, list_objects, parse_json, put_object


def test_concurrent_publication_is_immutable_and_private(tmp_path):
    root = tmp_path / "private"
    value = {"kind": "test", "quantity": "0.000000001", "missing_cash": None}
    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = list(pool.map(lambda _: put_object(root, value), range(12)))
    assert len(set(identities)) == 1
    identity = identities[0]
    assert list_objects(root) == [identity]
    assert get_object(root, identity) == value
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / (identity + ".json")).stat().st_mode & 0o777 == 0o600
    assert not list(root.glob(".record-*"))


def test_tampering_and_public_permissions_are_rejected(tmp_path):
    root = tmp_path / "private"
    identity = put_object(root, {"kind": "test"})
    path = root / (identity + ".json")
    path.write_text('{"tampered":true}')
    with pytest.raises(DataError, match="hash mismatch"):
        get_object(root, identity)
    with pytest.raises(DataError, match="collision"):
        put_object(root, {"kind": "test"})
    path.chmod(0o644)
    with pytest.raises(DataError, match="private regular"):
        get_object(root, identity)


def test_public_directory_and_symlinks_refused(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    with pytest.raises(DataError, match="0700"):
        put_object(root, {"kind": "test"})
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(DataError):
        put_object(link, {"kind": "test"})


def test_symlink_and_nonregular_objects_refused(tmp_path):
    root = tmp_path / "private"
    identity = put_object(root, {"kind": "test"})
    target = root / (identity + ".json")
    saved = root / "moved"
    target.rename(saved)
    target.symlink_to(saved)
    with pytest.raises(DataError):
        get_object(root, identity)
    target.unlink()
    os.mkfifo(target, mode=0o600)
    with pytest.raises(DataError, match="regular"):
        get_object(root, identity)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":1e999}', b"[]"])
def test_invalid_json_rejected_without_content(raw):
    with pytest.raises(DataError):
        parse_json(raw)


@pytest.mark.parametrize("identity", ["../private", "A" * 64, "abc", None])
def test_invalid_id_never_reads_paths(tmp_path, identity):
    with pytest.raises(DataError, match="Record ID"):
        get_object(tmp_path, identity)
