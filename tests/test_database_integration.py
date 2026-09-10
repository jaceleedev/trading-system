import json
import os
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from trading_research.config import Settings
from trading_research.data import Bundle, DataError, import_bundle, load_bundle, read_bundle
from trading_research.database import get_engine
from trading_research.demo import generate_demo


@pytest.fixture
def session():
    if os.environ.get("TRADING_TEST_DB") != "1":
        pytest.skip("Set TRADING_TEST_DB=1 to test the isolated local database")
    settings = Settings.from_env()
    assert settings.database_url.host in {"localhost", "127.0.0.1"}
    assert settings.database_url.port == 55432
    assert settings.database_url.database == "trading"
    with get_engine(settings).connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            yield session
        transaction.rollback()


@pytest.mark.integration
def test_atomic_import_roundtrip_and_immutable_revision(session, tmp_path):
    directory = generate_demo(tmp_path / "data")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataset_id"] = f"integration-{uuid4().hex}"
    manifest_path.write_text(json.dumps(manifest))
    bundle = load_bundle(directory)
    assert import_bundle(session, bundle)
    assert not import_bundle(session, bundle)
    restored = read_bundle(session, bundle.id)
    assert restored.sha256 == bundle.sha256
    assert sorted(restored.bars, key=lambda b: (b.instrument_id, b.session_date)) == sorted(
        bundle.bars, key=lambda b: (b.instrument_id, b.session_date)
    )
    changed = Bundle(bundle.manifest, "0" * 64, bundle.instruments, bundle.bars, bundle.fx)
    with pytest.raises(DataError, match="different content"):
        import_bundle(session, changed)
