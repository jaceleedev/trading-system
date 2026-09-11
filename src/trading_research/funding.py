"""Atomic local plan reservations, never brokerage cash or execution authorization."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from functools import wraps
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading_research.errors import DataError
from trading_research.jobs import (
    _HEX,
    JobStoreUnavailable,
    _id,
    _integer,
    _json,
    _name,
    _now,
    _text,
)
from trading_research.models import (
    CapitalPlanRegistrationRow,
    FundingPoolRow,
    FundingReservationLineRow,
    FundingReservationRow,
)
from trading_research.serialization import fingerprint

_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class FundingStoreUnavailable(JobStoreUnavailable):
    pass


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with localcontext(Context(prec=1024)):
                return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise FundingStoreUnavailable(
                "Local funding operation failed; details omitted"
            ) from None

    return wrapped


def _sha(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise DataError("Funding references require SHA-256 identities")
    return value


def _account(value):
    if (
        type(value) is not str
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
        or int(value) > 2**63 - 1
    ):
        raise DataError("Funding requires a positive signed64 account identifier string")
    return value


def _mode(value):
    if type(value) is not str or value not in {"prospective", "synthetic"}:
        raise DataError("Only prospective or isolated synthetic plans may reserve local resources")
    return value


def _instant(value):
    try:
        if type(value) is str:
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
            raise ValueError
        return value.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Funding observations require a UTC timestamp") from None


def _decimal(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not str or len(value) > 600 or not _DECIMAL.fullmatch(value):
        raise DataError("Funding values require bounded nonnegative decimal strings")
    return Decimal(value)


def _number(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _basis(value):
    value = _json(value)

    def check(item):
        if isinstance(item, float):
            raise DataError("Funding JSON must preserve decimals as text")
        if isinstance(item, dict):
            for child in item.values():
                check(child)
        if isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    return value


def _versions(value):
    value = _basis(value)
    if len(value) > 300:
        raise DataError("Funding pool revision map is too large")
    for identity, revision in value.items():
        _sha(identity)
        _integer(revision, 0, 2**31 - 1, "funding pool revision")
    return value


def _resource(kind, currency, market=None, symbol=None):
    if type(currency) is not str or currency not in {"KRW", "USD"}:
        raise DataError("Funding currency must be KRW or USD")
    if kind == "cash" and market is None and symbol is None:
        return {"kind": kind, "currency": currency, "market": None, "symbol": None}
    if kind == "holding" and type(market) is str and market in {"KR", "US"}:
        _name(symbol, 64, "holding symbol")
        return {"kind": kind, "currency": currency, "market": market, "symbol": symbol}
    raise DataError("Funding resource identity is invalid")


def pool_id(workspace_key, account_seq, kind, currency, market=None, symbol=None):
    """Stable identity across snapshots; holding currency cannot create a second inventory."""
    _sha(workspace_key)
    account_seq = _account(account_seq)
    resource = _resource(kind, currency, market, symbol)
    identity = {"currency": currency} if kind == "cash" else {"market": market, "symbol": symbol}
    return fingerprint(
        {
            "workspace_key": workspace_key,
            "provider": "toss",
            "account_seq": account_seq,
            "kind": resource["kind"],
            **identity,
        }
    )


def _vectors(namespace, account, values, *, nullable=False):
    values = _basis(values)
    if set(values) != {"cash", "holdings"}:
        raise DataError("Funding resources require cash and holdings lists")
    resources, normalized = {}, {"cash": [], "holdings": []}
    for field, kind, amount_field in (
        ("cash", "cash", "amount"),
        ("holdings", "holding", "quantity"),
    ):
        items = values[field]
        if type(items) is not list or len(items) > (2 if kind == "cash" else 100):
            raise DataError("Funding resource list exceeds its limit")
        keys = {"currency", amount_field} | ({"market", "symbol"} if kind == "holding" else set())
        for item in items:
            if type(item) is not dict or set(item) != keys:
                raise DataError("Funding resource fields are invalid")
            resource = _resource(kind, item["currency"], item.get("market"), item.get("symbol"))
            identity = pool_id(namespace, account, **resource)
            if identity in resources:
                raise DataError("Funding resource appears more than once")
            amount = _decimal(item[amount_field], nullable=nullable)
            resources[identity] = {**resource, "capacity": amount}
            normalized[field].append({**item, amount_field: _number(amount)})
        normalized[field].sort(
            key=lambda item: (item["currency"], item.get("market", ""), item.get("symbol", ""))
        )
    return resources, normalized


def _reservation(row):
    return {
        "id": row.id,
        "account_seq": row.account_seq,
        "plan_id": row.plan_id,
        "alternative_id": row.alternative_id,
        "request_key": row.request_key,
        "mode": row.mode,
        "status": row.status,
        "requirements": _basis(row.requirements),
        "pool_revisions": _basis(row.pool_revisions),
        "snapshot_ids": list(row.snapshot_ids),
        "created_at": _text(row.created_at),
        "released_at": _text(row.released_at),
        "replaced_by": row.replaced_by,
        "execution_ready": False,
    }


def _registration(row):
    return {
        "plan_id": row.plan_id,
        "request_key": row.request_key,
        "request_sha256": row.request_sha256,
        "created_at": _text(row.created_at),
    }


class FundingStore:
    def __init__(self, engine, workspace_key):
        self.engine, self.workspace_key = engine, _sha(workspace_key)

    def pool_id(self, account_seq, kind, currency, market=None, symbol=None):
        return pool_id(self.workspace_key, account_seq, kind, currency, market, symbol)

    @_safe
    def register_plan(self, plan_id, request_key, request_sha256):
        plan, key, digest = (
            _sha(plan_id),
            _name(request_key, 128, "request key"),
            _sha(request_sha256),
        )
        with Session(self.engine) as session, session.begin():
            session.execute(
                insert(CapitalPlanRegistrationRow)
                .values(
                    workspace_key=self.workspace_key,
                    plan_id=plan,
                    request_key=key,
                    request_sha256=digest,
                    created_at=_now(session),
                )
                .on_conflict_do_nothing(constraint="uq_capital_plan_request")
            )
            row = session.scalar(
                select(CapitalPlanRegistrationRow).where(
                    CapitalPlanRegistrationRow.workspace_key == self.workspace_key,
                    CapitalPlanRegistrationRow.request_key == key,
                )
            )
            if row.request_sha256 != digest:
                raise DataError("Capital plan request key identifies different input")
            return _registration(row)

    @_safe
    def find_plan_request(self, request_key):
        key = _name(request_key, 128, "request key")
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(CapitalPlanRegistrationRow).where(
                    CapitalPlanRegistrationRow.workspace_key == self.workspace_key,
                    CapitalPlanRegistrationRow.request_key == key,
                )
            )
            return _registration(row) if row else None

    @_safe
    def list_plans(self, limit=50):
        _integer(limit, 1, 100, "capital plan list limit")
        with Session(self.engine) as session, session.begin():
            condition = CapitalPlanRegistrationRow.workspace_key == self.workspace_key
            rows = list(
                session.execute(
                    select(CapitalPlanRegistrationRow, func.count().over())
                    .where(condition)
                    .order_by(
                        CapitalPlanRegistrationRow.created_at.desc(),
                        CapitalPlanRegistrationRow.request_key,
                    )
                    .limit(limit)
                ).all()
            )
            total = rows[0][1] if rows else 0
            return {
                "items": [_registration(row) for row, _ in rows],
                "total_count": total,
                "omitted_count": max(0, total - limit),
            }

    def _lock(self, session, account, *, shared=False):
        identity = fingerprint(
            {
                "lock": "trading-funding-account-v1",
                "workspace": self.workspace_key,
                "account": account,
            }
        )
        key = int.from_bytes(bytes.fromhex(identity[:16]), "big", signed=True)
        function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        session.execute(text(f"SELECT {function}(:key)"), {"key": key})

    def _request_lock(self, session, request_key):
        identity = fingerprint(
            {
                "lock": "trading-funding-request-v1",
                "workspace": self.workspace_key,
                "request_key": request_key,
            }
        )
        key = int.from_bytes(bytes.fromhex(identity[:16]), "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def _pools(self, session, account, *, lock=False):
        query = (
            select(FundingPoolRow)
            .where(
                FundingPoolRow.workspace_key == self.workspace_key,
                FundingPoolRow.account_seq == account,
            )
            .order_by(FundingPoolRow.id)
        )
        return {row.id: row for row in session.scalars(query.with_for_update() if lock else query)}

    def _held(self, session, account, excluding=None):
        query = (
            select(FundingReservationLineRow.pool_id, func.sum(FundingReservationLineRow.amount))
            .join(
                FundingReservationRow,
                FundingReservationRow.id == FundingReservationLineRow.reservation_id,
            )
            .where(
                FundingReservationRow.workspace_key == self.workspace_key,
                FundingReservationRow.account_seq == account,
                FundingReservationRow.status == "active",
            )
            .group_by(FundingReservationLineRow.pool_id)
        )
        if excluding is not None:
            query = query.where(FundingReservationRow.id != excluding)
        return dict(session.execute(query).all())

    def _state(self, session, account):
        session.flush()
        held = self._held(session, account)
        pools = []
        for row in self._pools(session, account).values():
            reserved = held.get(row.id, Decimal(0))
            pools.append(
                {
                    "id": row.id,
                    "kind": row.kind,
                    "currency": row.currency,
                    "market": row.market,
                    "symbol": row.symbol,
                    "mode": row.mode,
                    "snapshot_id": row.snapshot_id,
                    "observed_at": _text(row.observed_at),
                    "capacity": _number(row.capacity),
                    "reserved": _number(reserved),
                    "available": _number(max(Decimal(0), row.capacity - reserved))
                    if row.capacity is not None
                    else None,
                    "overallocated": reserved > row.capacity if row.capacity is not None else None,
                    "revision": row.revision,
                    "basis": _basis(row.basis),
                }
            )
        total = session.scalar(
            select(func.count())
            .select_from(FundingReservationRow)
            .where(
                FundingReservationRow.workspace_key == self.workspace_key,
                FundingReservationRow.account_seq == account,
            )
        )
        reservations = list(
            session.scalars(
                select(FundingReservationRow)
                .where(
                    FundingReservationRow.workspace_key == self.workspace_key,
                    FundingReservationRow.account_seq == account,
                )
                .order_by(FundingReservationRow.created_at.desc(), FundingReservationRow.id)
                .limit(101)
            )
        )
        return {
            "account_seq": account,
            "provider": "toss",
            "pools": pools,
            "reservations": [_reservation(row) for row in reservations[:100]],
            "omitted_reservation_count": max(0, total - 100),
            "execution_ready": False,
        }

    @_safe
    def state(self, account_seq):
        account = _account(account_seq)
        with Session(self.engine) as session, session.begin():
            self._lock(session, account, shared=True)
            return self._state(session, account)

    @_safe
    def refresh(
        self,
        account_seq,
        snapshot_id,
        observed_at,
        capacities,
        expected_revisions,
        mode,
        *,
        basis=None,
    ):
        account, snapshot, observed, mode = (
            _account(account_seq),
            _sha(snapshot_id),
            _instant(observed_at),
            _mode(mode),
        )
        resources, _ = _vectors(self.workspace_key, account, capacities, nullable=True)
        versions, basis = _versions(expected_revisions), _basis(basis or {})
        with Session(self.engine) as session, session.begin():
            self._lock(session, account)
            existing = self._pools(session, account, lock=True)
            now = _now(session)
            if observed > now:
                raise DataError("Funding observation cannot be in the future")
            if set(resources) <= set(existing) and all(
                (row.snapshot_id, row.observed_at, row.capacity, row.basis, row.mode)
                == (
                    snapshot,
                    observed,
                    resources[key]["capacity"] if key in resources else None,
                    basis,
                    mode,
                )
                and (key not in resources or row.currency == resources[key]["currency"])
                for key, row in existing.items()
            ):
                return self._state(session, account)
            if set(versions) != set(resources) | set(existing):
                raise DataError("Refresh requires revisions for every existing and proposed pool")
            for identity in sorted(set(resources) | set(existing)):
                row = existing.get(identity)
                resource = resources.get(identity)
                if versions[identity] != (row.revision if row else 0):
                    raise DataError("Funding pool revision changed")
                if row:
                    if row.mode != mode:
                        raise DataError("Funding modes cannot share an operational account pool")
                    if observed < row.observed_at or (
                        observed == row.observed_at and snapshot != row.snapshot_id
                    ):
                        raise DataError(
                            "Funding observation is stale or conflicts at the same time"
                        )
                    if resource and row.currency != resource["currency"]:
                        raise DataError(
                            "Holding currency changed without an explicit reconciliation"
                        )
                    capacity = resource["capacity"] if resource else None
                    if (row.snapshot_id, row.observed_at, row.capacity, row.basis) == (
                        snapshot,
                        observed,
                        capacity,
                        basis,
                    ):
                        continue
                    row.snapshot_id, row.observed_at, row.capacity, row.basis = (
                        snapshot,
                        observed,
                        capacity,
                        basis,
                    )
                    row.revision += 1
                    row.updated_at = now
                else:
                    session.add(
                        FundingPoolRow(
                            id=identity,
                            workspace_key=self.workspace_key,
                            provider="toss",
                            account_seq=account,
                            mode=mode,
                            snapshot_id=snapshot,
                            observed_at=observed,
                            revision=1,
                            basis=basis,
                            created_at=now,
                            updated_at=now,
                            **resource,
                        )
                    )
            return self._state(session, account)

    def _lookup(self, session, identity):
        return session.scalar(
            select(FundingReservationRow).where(
                FundingReservationRow.id == identity,
                FundingReservationRow.workspace_key == self.workspace_key,
            )
        )

    @_safe
    def get(self, reservation_id):
        identity = _id(reservation_id)
        with Session(self.engine) as session, session.begin():
            row = self._lookup(session, identity)
            return _reservation(row) if row else None

    get_reservation = get

    @_safe
    def find_reservation_request(self, request_key):
        key = _name(request_key, 128, "request key")
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(FundingReservationRow).where(
                    FundingReservationRow.workspace_key == self.workspace_key,
                    FundingReservationRow.request_key == key,
                )
            )
            return _reservation(row) if row else None

    @_safe
    def reserve(
        self,
        account_seq,
        plan_id,
        alternative_id,
        requirements,
        request_key,
        expected_revisions,
        mode,
    ):
        return self._reserve(
            account_seq,
            plan_id,
            alternative_id,
            requirements,
            request_key,
            expected_revisions,
            mode,
        )

    def _reserve(
        self,
        account_seq,
        plan_id,
        alternative_id,
        requirements,
        request_key,
        expected_revisions,
        mode,
        replacing=None,
    ):
        account, plan, alternative = (
            _account(account_seq),
            _sha(plan_id),
            _name(alternative_id, 64, "alternative"),
        )
        mode, key, versions = (
            _mode(mode),
            _name(request_key, 128, "request key"),
            _versions(expected_revisions),
        )
        resources, normalized = _vectors(self.workspace_key, account, requirements)
        resources = {
            identity: value for identity, value in resources.items() if value["capacity"] > 0
        }
        digest = fingerprint(
            {
                "account_seq": account,
                "plan_id": plan,
                "alternative_id": alternative,
                "requirements": normalized,
                "mode": mode,
                "expected_revisions": versions,
                "replacing": replacing,
            }
        )
        with Session(self.engine) as session, session.begin():
            # Request keys span accounts; serialize before the account lock so
            # simultaneous cross-account key reuse remains a conflict, not a DB outage.
            self._request_lock(session, key)
            self._lock(session, account)
            if (
                session.scalar(
                    select(CapitalPlanRegistrationRow.plan_id)
                    .where(
                        CapitalPlanRegistrationRow.workspace_key == self.workspace_key,
                        CapitalPlanRegistrationRow.plan_id == plan,
                    )
                    .limit(1)
                )
                is None
            ):
                raise DataError("Reservation plan is not registered in this workspace")
            duplicate = session.scalar(
                select(FundingReservationRow).where(
                    FundingReservationRow.workspace_key == self.workspace_key,
                    FundingReservationRow.request_key == key,
                )
            )
            if duplicate:
                if duplicate.request_sha256 != digest:
                    raise DataError("Funding request key already identifies different input")
                return _reservation(duplicate)
            pools = self._pools(session, account, lock=True)
            if any(row.mode != mode for row in pools.values()):
                raise DataError("Reservation mode differs from its account pool")
            previous = self._lookup(session, replacing) if replacing else None
            if replacing and (
                previous is None or previous.account_seq != account or previous.status != "active"
            ):
                raise DataError("Replacement requires an active reservation in this account")
            touched = set(resources)
            if previous:
                touched.update(
                    session.scalars(
                        select(FundingReservationLineRow.pool_id).where(
                            FundingReservationLineRow.reservation_id == previous.id
                        )
                    )
                )
            if not touched <= set(versions):
                raise DataError("Reservation requires every affected pool revision")
            held = self._held(session, account, excluding=replacing)
            for identity in sorted(touched):
                row = pools.get(identity)
                if row is None or row.revision != versions[identity]:
                    raise DataError("Funding pool is missing or its revision changed")
                if row.mode != mode:
                    raise DataError("Reservation mode differs from its account pool")
                demand = resources.get(identity)
                if demand and (
                    row.currency != demand["currency"]
                    or row.capacity is None
                    or demand["capacity"] + held.get(identity, Decimal(0)) > row.capacity
                ):
                    raise DataError("Reservation exceeds known available local capacity")
            now, identity = _now(session), str(uuid4())
            result = FundingReservationRow(
                id=identity,
                workspace_key=self.workspace_key,
                account_seq=account,
                plan_id=plan,
                alternative_id=alternative,
                request_key=key,
                request_sha256=digest,
                mode=mode,
                status="active",
                requirements=normalized,
                pool_revisions={key: pools[key].revision for key in sorted(touched)},
                snapshot_ids=sorted({pools[key].snapshot_id for key in touched}),
                created_at=now,
                released_at=None,
                replaced_by=None,
            )
            session.add(result)
            session.flush()
            for key, value in resources.items():
                session.add(
                    FundingReservationLineRow(
                        reservation_id=identity, pool_id=key, amount=value["capacity"]
                    )
                )
            if previous:
                previous.status, previous.released_at, previous.replaced_by = (
                    "replaced",
                    now,
                    identity,
                )
            for key in touched:
                pools[key].revision += 1
                pools[key].updated_at = now
            session.flush()
            return _reservation(result)

    @_safe
    def release(self, reservation_id):
        identity = _id(reservation_id)
        with Session(self.engine) as session, session.begin():
            row = self._lookup(session, identity)
            if row is None:
                return None
            self._lock(session, row.account_seq)
            session.refresh(row)
            if row.status != "active":
                return _reservation(row)
            pools = self._pools(session, row.account_seq, lock=True)
            now = _now(session)
            for key in session.scalars(
                select(FundingReservationLineRow.pool_id).where(
                    FundingReservationLineRow.reservation_id == identity
                )
            ):
                pools[key].revision += 1
                pools[key].updated_at = now
            row.status, row.released_at = "released", now
            return _reservation(row)

    @_safe
    def replace(
        self,
        reservation_id,
        plan_id,
        alternative_id,
        requirements,
        request_key,
        expected_revisions,
        mode,
    ):
        identity = _id(reservation_id)
        with Session(self.engine) as session, session.begin():
            row = self._lookup(session, identity)
            if row is None:
                return None
            account = row.account_seq
        return self._reserve(
            account,
            plan_id,
            alternative_id,
            requirements,
            request_key,
            expected_revisions,
            mode,
            replacing=identity,
        )
