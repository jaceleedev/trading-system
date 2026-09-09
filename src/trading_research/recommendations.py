from sqlalchemy.orm import Session

from trading_research.data import DataError, timestamp
from trading_research.models import RecommendationRow
from trading_research.strategy import fingerprint


def save_recommendation(session: Session, payload: dict) -> bool:
    digest = fingerprint(payload)
    existing = session.get(RecommendationRow, payload["id"])
    if existing:
        if existing.payload_sha256 != digest:
            raise DataError("Recommendation identity collision; existing evidence is immutable")
        return False
    session.add(
        RecommendationRow(
            id=payload["id"],
            dataset_id=payload["dataset_id"],
            as_of=timestamp(payload["as_of"]),
            payload=payload,
            payload_sha256=digest,
        )
    )
    session.flush()
    return True


def checked_payload(row: RecommendationRow) -> dict:
    if fingerprint(row.payload) != row.payload_sha256:
        raise DataError("Stored recommendation checksum does not match")
    return row.payload
