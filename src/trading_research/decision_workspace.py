"""Immutable, source-linked judgments authored by this Codex task or the user.

This module validates records, not investment truth or model identity. Decisions
remain proposals; account buying power never becomes verified cash or sizing.
"""

import copy
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from trading_research.errors import DataError
from trading_research.private_store import (
    OBJECT_ID,
    get_object,
    list_objects,
    object_bytes,
    put_object,
)
from trading_research.toss_account import validate_observation, validate_snapshot

KINDS = {"evidence", "hypothesis", "decision", "review"}
MODES = {"prospective", "retrospective", "synthetic"}
_INPUT_KEYS = {"kind", "mode", "author", "payload"}
_ENVELOPE_KEYS = _INPUT_KEYS | {"schema_version", "recorded_at"}
_EXPOSURE_ACTIONS = {"buy", "add", "trim", "sell"}
_SUBJECT_ACTIONS = _EXPOSURE_ACTIONS | {"hold", "avoid"}
_ACTIONS = _SUBJECT_ACTIONS | {"research", "watch", "wait"}
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_DEFAULT_ACCOUNT_ROOT = Path("var/accounts")


def _fields(value, required, optional=(), label="record"):
    if type(value) is not dict or not required <= set(value) <= required | set(optional):
        raise DataError(f"Research {label} fields are invalid")


def _text(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value.strip() or len(value) > 20000:
        raise DataError("Research text must be nonempty and bounded")


def _choice(value, choices, label):
    if type(value) is not str or value not in choices:
        raise DataError(f"Research {label} is unsupported")


def _instant(value):
    if type(value) is str and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise DataError("Research timestamp is invalid") from None
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise DataError("Research timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _identity(value):
    if type(value) is not str or OBJECT_ID.fullmatch(value) is None:
        raise DataError("Research reference must be a lowercase SHA-256 digest")


def _items(value, *, identities=False, nonempty=False):
    if type(value) is not list or len(value) > 200 or (nonempty and not value):
        raise DataError("Research list is missing or exceeds its limit")
    for item in value:
        (_identity if identities else _text)(item)
    if identities and len(set(value)) != len(value):
        raise DataError("Research reference list contains duplicate identities")


def _author(author):
    _fields(author, {"interface", "model", "reasoning_effort", "identity_source"}, label="author")
    _choice(author["interface"], {"codex", "human"}, "author interface")
    _choice(author["identity_source"], {"declared", "unknown"}, "model identity source")
    _text(author["model"], nullable=True)
    _text(author["reasoning_effort"], nullable=True)
    if author["identity_source"] == "unknown" and (
        author["model"] is not None or author["reasoning_effort"] is not None
    ):
        raise DataError("Unknown model identity cannot include a model or reasoning effort")
    if author["interface"] == "human" and (
        author["model"] is not None or author["reasoning_effort"] is not None
    ):
        raise DataError("Human authors cannot declare model identity")


def _account(account_root, identity, deadline, *, snapshot_only=False):
    _identity(identity)
    artifact = get_object(account_root, identity)
    if artifact.get("kind") == "toss_account_snapshot":
        validate_snapshot(artifact)
        observed = artifact["collection_completed_at"]
    elif not snapshot_only and artifact.get("kind") == "toss_account_observation":
        validate_observation(artifact)
        observed = artifact["retrieved_at"]
    else:
        raise DataError("Research account reference has the wrong object kind")
    if _instant(observed) > deadline:
        raise DataError("Research account reference was observed after the record")
    return artifact


class _Reader:
    def __init__(self, root, account_root):
        self.root, self.account_root = Path(root), Path(account_root)
        self.cache, self.active = {}, set()

    def read(self, identity):
        _identity(identity)
        if identity in self.active or len(self.active) >= 50:
            raise DataError("Research reference graph contains a cycle or is too deep")
        if identity not in self.cache:
            if len(self.cache) + len(self.active) >= 2000:
                raise DataError("Research reference graph exceeds its size limit")
            self.active.add(identity)
            try:
                envelope = get_object(self.root, identity)
                self.validate(envelope)
                self.cache[identity] = envelope
            finally:
                self.active.remove(identity)
        return self.cache[identity]

    def ref(self, parent, identity, kind):
        child = self.read(identity)
        if child["kind"] != kind:
            raise DataError("Research reference has the wrong record kind")
        if _instant(child["recorded_at"]) > _instant(parent["recorded_at"]):
            raise DataError("Research dependency was recorded after its parent")
        if parent["mode"] == "prospective" and child["mode"] != "prospective":
            raise DataError("Prospective research cannot depend on retrospective or synthetic work")
        return child

    def refs(self, envelope, field, kind):
        values = envelope["payload"][field]
        _items(values, identities=True)
        return [self.ref(envelope, value, kind) for value in values]

    def validate(self, envelope):
        _fields(envelope, _ENVELOPE_KEYS)
        if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
            raise DataError("Research schema version is unsupported")
        _choice(envelope["kind"], KINDS, "kind")
        _choice(envelope["mode"], MODES, "mode")
        _instant(envelope["recorded_at"])
        _author(envelope["author"])
        getattr(self, envelope["kind"])(envelope)

    def evidence(self, envelope):
        payload = envelope["payload"]
        _fields(
            payload,
            {
                "source_kind",
                "source_locator",
                "retrieved_at",
                "source_published_at",
                "claim",
                "verification",
            },
            {"excerpt", "artifact"},
            "evidence",
        )
        _choice(payload["source_kind"], {"web", "document", "provider", "user"}, "source kind")
        _choice(
            payload["verification"],
            {"user_supplied", "provider_capture", "unverified"},
            "evidence verification",
        )
        _text(payload["source_locator"])
        _text(payload["claim"])
        if "excerpt" in payload:
            _text(payload["excerpt"])
        if payload["source_kind"] == "web":
            try:
                parsed = urlsplit(payload["source_locator"])
                valid = (
                    parsed.scheme == "https"
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port in (None, 443)
                    and not any(c.isspace() or ord(c) < 32 for c in payload["source_locator"])
                )
            except ValueError:
                valid = False
            if not valid:
                raise DataError("Research web source must be an HTTPS URL without credentials")
        retrieved = _instant(payload["retrieved_at"])
        if retrieved > _instant(envelope["recorded_at"]):
            raise DataError("Research evidence retrieval is later than its record")
        if payload["source_published_at"] is not None:
            if _instant(payload["source_published_at"]) > retrieved:
                raise DataError("Research claimed publication is later than retrieval")
        if "artifact" in payload:
            artifact = payload["artifact"]
            _fields(artifact, {"store", "id"}, label="evidence artifact")
            if artifact["store"] != "account" or payload["source_kind"] != "provider":
                raise DataError("Research artifacts support account provider objects only")
            source = _account(self.account_root, artifact["id"], retrieved)
            locator = (
                "toss:account_snapshot"
                if source["kind"] == "toss_account_snapshot"
                else "toss:" + source["endpoint"]
            )
            if payload["source_locator"] != locator:
                raise DataError("Research provider source locator does not match its artifact")
        if payload["verification"] == "provider_capture" and (
            payload["source_kind"] != "provider" or "artifact" not in payload
        ):
            raise DataError("Provider capture verification requires a validated account artifact")

    def hypothesis(self, envelope):
        payload = envelope["payload"]
        _fields(
            payload,
            {
                "subject",
                "thesis",
                "supporting_evidence_ids",
                "opposing_evidence_ids",
                "uncertainties",
                "invalidation_conditions",
                "review_triggers",
            },
            {"supersedes_id"},
            "hypothesis",
        )
        _text(payload["subject"])
        _text(payload["thesis"])
        for field in ("uncertainties", "invalidation_conditions", "review_triggers"):
            _items(payload[field])
        for field in ("supporting_evidence_ids", "opposing_evidence_ids"):
            self.refs(envelope, field, "evidence")
        if "supersedes_id" in payload:
            self.ref(envelope, payload["supersedes_id"], "hypothesis")

    def decision(self, envelope):
        payload = envelope["payload"]
        _fields(
            payload,
            {
                "objective",
                "hypothesis_ids",
                "evidence_ids",
                "account_snapshot_id",
                "alternatives",
                "proposed_actions",
                "rationale",
                "unresolved_questions",
                "review_after",
                "status",
                "sizing_validated",
            },
            {"prior_decision_id"},
            "decision",
        )
        if payload["status"] != "proposed" or payload["sizing_validated"] is not False:
            raise DataError("Research decisions must remain proposals with unvalidated sizing")
        _text(payload["objective"])
        _text(payload["rationale"])
        _items(payload["alternatives"], nonempty=True)
        _items(payload["unresolved_questions"])
        if _instant(payload["review_after"]) < _instant(envelope["recorded_at"]):
            raise DataError("Research review time cannot precede the decision")
        self.refs(envelope, "hypothesis_ids", "hypothesis")
        self.refs(envelope, "evidence_ids", "evidence")
        if "prior_decision_id" in payload:
            self.ref(envelope, payload["prior_decision_id"], "decision")
        if payload["account_snapshot_id"] is not None:
            _account(
                self.account_root,
                payload["account_snapshot_id"],
                _instant(envelope["recorded_at"]),
                snapshot_only=True,
            )
        actions = payload["proposed_actions"]
        if type(actions) is not list or not 1 <= len(actions) <= 100:
            raise DataError("Research decision requires a bounded list of proposed actions")
        subjects = set()
        for action in actions:
            _action(action)
            if action["action"] in _SUBJECT_ACTIONS and not (
                payload["hypothesis_ids"] or payload["evidence_ids"]
            ):
                raise DataError("Research investment action requires referenced reasoning")
            if action["action"] in _EXPOSURE_ACTIONS and payload["account_snapshot_id"] is None:
                raise DataError("Research exposure change requires an account snapshot")
            if action["symbol"] is not None:
                subject = (action["market"], action["symbol"])
                if subject in subjects:
                    raise DataError("Research decision contains conflicting or duplicate subjects")
                subjects.add(subject)

    def review(self, envelope):
        payload = envelope["payload"]
        _fields(
            payload,
            {"decision_id", "new_evidence_ids", "observations", "what_changed", "judgment"},
            {"replacement_decision_id"},
            "review",
        )
        decision = self.ref(envelope, payload["decision_id"], "decision")
        self.refs(envelope, "new_evidence_ids", "evidence")
        _items(payload["observations"], nonempty=True)
        _text(payload["what_changed"])
        _choice(
            payload["judgment"], {"maintain", "revise", "retire", "unresolved"}, "review judgment"
        )
        if "replacement_decision_id" in payload:
            replacement = self.ref(envelope, payload["replacement_decision_id"], "decision")
            if (
                payload["judgment"] != "revise"
                or replacement["payload"].get("prior_decision_id") != payload["decision_id"]
                or _instant(replacement["recorded_at"]) < _instant(decision["recorded_at"])
            ):
                raise DataError("Research replacement decision does not match the review lineage")


def _action(action):
    _fields(
        action, {"action", "market", "symbol", "rationale"}, {"target_weight", "quantity"}, "action"
    )
    _choice(action["action"], _ACTIONS, "action")
    _text(action["rationale"])
    if (action["market"] is None) != (action["symbol"] is None):
        raise DataError("Research action market and symbol must be supplied together")
    if action["symbol"] is not None:
        _choice(action["market"], {"KR", "US"}, "market")
        if type(action["symbol"]) is not str or _SYMBOL.fullmatch(action["symbol"]) is None:
            raise DataError("Research action symbol is invalid")
    elif action["action"] in _SUBJECT_ACTIONS:
        raise DataError("Research investment action requires an explicit market and symbol")
    sizes = {"target_weight", "quantity"} & set(action)
    if len(sizes) > 1 or (sizes and action["action"] not in _EXPOSURE_ACTIONS):
        raise DataError("Research action permits one sizing proposal for an exposure change")
    for field in sizes:
        value = action[field]
        if type(value) is not str or len(value) > 64 or _DECIMAL.fullmatch(value) is None:
            raise DataError("Research sizing requires a bounded nonnegative decimal string")
        amount = Decimal(value)
        if (field == "quantity" and amount <= 0) or (field == "target_weight" and amount > 1):
            raise DataError("Research proposed sizing is outside its valid range")


def record(root, document, *, account_root=_DEFAULT_ACCOUNT_ROOT, now=None):
    """Validate and append a system-stamped record; never invoke a model or order API."""
    object_bytes(document)
    _fields(document, _INPUT_KEYS, label="input")
    instant = datetime.now(UTC) if now is None else (now() if callable(now) else now)
    envelope = {
        **copy.deepcopy(document),
        "schema_version": 1,
        "recorded_at": _instant(instant).isoformat(),
    }
    if envelope["kind"] == "decision":
        payload = envelope["payload"]
        if type(payload) is not dict or {"status", "sizing_validated"} & set(payload):
            raise DataError("Research decision status and sizing validation are system supplied")
        payload.update(status="proposed", sizing_validated=False)
    _Reader(root, account_root).validate(envelope)
    return {"id": put_object(Path(root), envelope), "record": envelope}


def read_record(root, identity, *, account_root=_DEFAULT_ACCOUNT_ROOT):
    """Revalidate the full immutable lineage at its recorded time, not today's clock."""
    return _Reader(root, account_root).read(identity)


def list_records(root, kind=None, *, account_root=_DEFAULT_ACCOUNT_ROOT):
    """List validated records; corrupt dependencies never disappear behind a filter."""
    if kind is not None:
        _choice(kind, KINDS, "kind filter")
    reader = _Reader(root, account_root)
    result = []
    for identity in list_objects(Path(root)):
        envelope = reader.read(identity)
        if kind is None or envelope["kind"] == kind:
            label = {
                "evidence": "claim",
                "hypothesis": "subject",
                "decision": "objective",
                "review": "what_changed",
            }[envelope["kind"]]
            result.append(
                {
                    "id": identity,
                    "kind": envelope["kind"],
                    "mode": envelope["mode"],
                    "recorded_at": envelope["recorded_at"],
                    "subject": envelope["payload"][label],
                }
            )
    return sorted(result, key=lambda item: (_instant(item["recorded_at"]), item["id"]))
