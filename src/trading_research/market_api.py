"""Bounded, offline market projections shared by the web workbench and Codex tools."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Query
from pydantic import AwareDatetime, Field

from trading_research.api_models import (
    APIModel,
    DecimalText,
    ErrorResponse,
    ObjectId,
    TimestampText,
)
from trading_research.errors import DataError


class MarketCaptureSummary(APIModel):
    capture_id: ObjectId
    endpoint: str
    symbol: str | None
    interval: str | None
    adjusted: bool | None
    currencies: list[str]
    retrieved_at: TimestampText
    candle_count: int | None
    status: Literal["supported", "unsupported"]
    reason: str | None
    response_contract_sha256: ObjectId | None


class MarketCatalog(APIModel):
    items: list[MarketCaptureSummary]
    total_count: int
    supported_count: int
    unsupported_count: int
    invalid_count: int
    truncated_count: int


class CandleRevision(APIModel):
    id: ObjectId
    observed_at: TimestampText
    last_observed_at: TimestampText
    capture_ids: list[ObjectId]
    open: DecimalText
    high: DecimalText
    low: DecimalText
    close: DecimalText
    volume: DecimalText


class ObservedCandle(CandleRevision):
    source_timestamp: TimestampText
    period_start: TimestampText | None
    period_end: TimestampText | None
    session_date: str | None
    revision_count: int
    finality: Literal["unknown"]
    revisions: list[CandleRevision]


class MarketSeries(APIModel):
    id: ObjectId
    provider: Literal["toss"]
    symbol: str
    currency: str
    interval: Literal["1m", "1d"]
    adjusted: bool
    points: list[ObservedCandle]


class MarketEvidenceEvent(APIModel):
    record_id: ObjectId
    symbol: str
    market: Literal["KR", "US"]
    event_kind: Literal["price", "earnings", "filing", "news", "macro", "other"]
    occurred_at: TimestampText | None
    source_published_at: TimestampText | None
    retrieved_at: TimestampText
    recorded_at: TimestampText
    claim: str
    mode: Literal["prospective", "retrospective", "synthetic"]
    source_locator: str
    verification: Literal["user_supplied", "provider_capture", "unverified"]


class MarketView(APIModel):
    schema_version: Literal[1]
    kind: Literal["market_observation_view"]
    id: ObjectId
    as_of: TimestampText
    generated_at: TimestampText
    source_capture_ids: list[ObjectId]
    excluded_future_capture_ids: list[ObjectId]
    response_contract_sha256: ObjectId
    series: list[MarketSeries]
    total_point_count: int
    truncated_point_count: int
    historical_reproducibility: Literal[False]
    orders_enabled: Literal[False]
    warnings: list[str]
    events: list[MarketEvidenceEvent]
    event_count: int
    omitted_event_count: int


class MarketViewRequest(APIModel):
    capture_ids: Annotated[list[ObjectId], Field(min_length=1, max_length=100)]
    as_of: Annotated[AwareDatetime | None, Field(strict=False)] = None
    max_points: Annotated[int, Field(ge=1, le=2000)] = 1000
    max_events: Annotated[int, Field(ge=0, le=200)] = 200


def _check_workspace(workspace):
    workspace = Path(workspace)
    for path in (
        workspace,
        workspace / "var",
        *(workspace / "var" / name for name in ("captures", "research", "accounts")),
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DataError("Market workspace stores are unavailable or unsafe")
    return workspace


def market_catalog(workspace, *, limit=100):
    from trading_research.market_observations import catalog

    workspace = _check_workspace(workspace)
    return catalog(workspace / "var/captures", limit=limit)


def market_view(workspace, capture_ids, *, as_of=None, max_points=1000, max_events=200):
    from trading_research.decision_workspace import list_records, read_record
    from trading_research.market_observations import build_view

    if type(max_events) is not int or not 0 <= max_events <= 200:
        raise DataError("Market event limit must be an integer from 0 through 200")
    workspace = _check_workspace(workspace)
    captures, research, accounts = (
        workspace / "var" / name for name in ("captures", "research", "accounts")
    )
    view = build_view(captures, capture_ids, as_of=as_of, max_points=max_points)
    deadline = datetime.fromisoformat(view["as_of"])
    symbols = {series["symbol"] for series in view["series"]}
    events = []
    for summary in list_records(research, "evidence", account_root=accounts, capture_root=captures):
        if datetime.fromisoformat(summary["recorded_at"]) > deadline:
            continue
        record = read_record(research, summary["id"], account_root=accounts, capture_root=captures)
        payload = record["payload"]
        event = payload.get("market_event")
        if event is None or event["symbol"] not in symbols:
            continue
        if datetime.fromisoformat(payload["retrieved_at"]) > deadline:
            continue
        events.append(
            {
                "record_id": summary["id"],
                **event,
                "recorded_at": record["recorded_at"],
                "mode": record["mode"],
                **{
                    key: payload[key]
                    for key in (
                        "source_published_at",
                        "retrieved_at",
                        "claim",
                        "source_locator",
                        "verification",
                    )
                },
            }
        )
    events.sort(key=lambda item: (datetime.fromisoformat(item["recorded_at"]), item["record_id"]))
    return {
        **view,
        "events": events[-max_events:] if max_events else [],
        "event_count": len(events),
        "omitted_event_count": max(0, len(events) - max_events),
    }


def register_market_routes(app, workspace):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 409, 422, 500)}

    @app.get(
        "/api/v1/market/catalog",
        response_model=MarketCatalog,
        operation_id="list_market_captures",
        responses=responses,
    )
    def list_market_captures(limit: Annotated[int, Query(ge=1, le=100)] = 100):
        return market_catalog(workspace, limit=limit)

    @app.post(
        "/api/v1/market/view",
        response_model=MarketView,
        operation_id="get_market_view",
        responses=responses,
    )
    def get_market_view(document: MarketViewRequest):
        return market_view(
            workspace,
            document.capture_ids,
            as_of=document.as_of,
            max_points=document.max_points,
            max_events=document.max_events,
        )
