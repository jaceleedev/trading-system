"""Pure V2 sizing proposals and references; never budget authority or execution.

Source checks operate on an already integrity-validated frozen input. They bind
references and times within that graph; they do not read files, verify a provider,
or certify a model's claims about the referenced contents.
"""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator

from trading_research.errors import DataError
from trading_research.private_store import OBJECT_ID, object_bytes

MAX_TOTAL_LEGS = 50
_V2 = json.loads(Path(__file__).with_name("investigation_output_v2.schema.json").read_text())
_VALIDATOR = Draft202012Validator(_V2["properties"]["capital_proposal"])
_NUMBERS = ("quantity", "price", "fee_bps", "fixed_fee", "tax_bps")
_LEG_FIELDS = (
    "action",
    "symbol",
    "market",
    "currency",
    *_NUMBERS,
    "rationale",
)


def validate_capital_proposal(proposal):
    """Return a validated copy with unknown values preserved as explicit nulls."""
    object_bytes({"capital_proposal": proposal})
    if not _VALIDATOR.is_valid(proposal):
        raise DataError("Capital proposal does not match its closed V2 schema")
    if proposal is None:
        return None
    alternatives = proposal["alternatives"]
    if len({item["key"] for item in alternatives}) != len(alternatives):
        raise DataError("Capital proposal alternative keys must be unique")
    if sum(len(item["legs"]) for item in alternatives) > MAX_TOTAL_LEGS:
        raise DataError("Capital proposal exceeds its total leg limit")
    for alternative in alternatives:
        if not alternative["label"].strip() or not alternative["rationale"].strip():
            raise DataError("Capital alternatives require nonempty explanations")
        for leg in alternative["legs"]:
            for field in ("rationale", "sizing_rationale", "price_rationale", "cost_rationale"):
                if not leg[field].strip():
                    raise DataError("Capital sizing, prices and costs require explanations")
            if {"KR": "KRW", "US": "USD"}[leg["market"]] != leg["currency"]:
                raise DataError("Capital proposal currency must match its market")
            if leg["quantity"] is not None and leg["action"] != "hold":
                if Decimal(leg["quantity"]) <= 0:
                    raise DataError("Capital exposure changes require positive quantities")
            if leg["price"] is not None and Decimal(leg["price"]) <= 0:
                raise DataError("Capital proposal prices must be positive or unknown")
            for field in ("fee_bps", "tax_bps"):
                if leg[field] is not None and Decimal(leg[field]) > 10000:
                    raise DataError("Capital proposal cost basis points exceed 10000")
            for field in ("evidence_ids", "capture_ids"):
                if len(set(leg[field])) != len(leg[field]):
                    raise DataError("Capital proposal references must be unique within each leg")
    return copy.deepcopy(proposal)


def _validated_output(output):
    # Kept lazy: the runner calls validate_capital_proposal after its versioned
    # schema checks, while these public helpers also accept complete V1 outputs.
    from trading_research.codex_runner import validate_output

    return validate_output(output)


def capital_completeness(output):
    """List missing calculation assumptions without inventing zero costs or prices."""
    value = _validated_output(output)
    proposal = value.get("capital_proposal")
    result = {"complete_alternative_keys": [], "incomplete_alternatives": []}
    if proposal is None:
        return result
    for alternative in proposal["alternatives"]:
        missing = []
        for index, leg in enumerate(alternative["legs"]):
            for field in _NUMBERS:
                if field == "price" and leg["action"] == "hold":
                    continue
                if leg[field] is None:
                    missing.append(f"legs[{index}].{field}")
        if missing:
            result["incomplete_alternatives"].append(
                {"key": alternative["key"], "missing_fields": missing}
            )
        else:
            result["complete_alternative_keys"].append(alternative["key"])
    return result


def capital_alternatives(output):
    """Project whole, complete alternatives to the existing capital-plan contract.

    Completeness is not affordability or eligibility. Source, mode, funding and
    request identity must be supplied by the controller, outside the model output.
    A missing field in one leg excludes the entire alternative, never only that leg.
    """
    value = _validated_output(output)
    proposal = value.get("capital_proposal")
    if proposal is None:
        return []
    complete = set(capital_completeness(value)["complete_alternative_keys"])
    return [
        {
            "key": alternative["key"],
            "label": alternative["label"],
            "rationale": alternative["rationale"],
            "legs": [{field: leg[field] for field in _LEG_FIELDS} for leg in alternative["legs"]],
        }
        for alternative in proposal["alternatives"]
        if alternative["key"] in complete
    ]


def _instant(value):
    try:
        if type(value) is not str or len(value) > 64:
            raise ValueError
        instant = datetime.fromisoformat(value)
        if instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(UTC)
    except ValueError, TypeError, OverflowError:
        raise DataError("Proposal source times require an explicit timezone") from None


def _identity(value):
    if type(value) is not str or OBJECT_ID.fullmatch(value) is None:
        raise DataError("Proposal source identity must be a SHA-256 digest")
    return value


def _before(value, deadline):
    value = _instant(value)
    if value > deadline:
        raise DataError("Proposal source is later than its frozen input")
    return value


def _evidence_sources(frozen, deadline, modes):
    evidence = set()
    for item in frozen["context"]["records"] + frozen["explicit_evidence"]:
        record = item["record"]
        if record["kind"] != "evidence":
            continue
        if record["mode"] not in modes:
            raise DataError("Proposal evidence mode differs from its frozen investigation")
        recorded = _before(record["recorded_at"], deadline)
        payload = record["payload"]
        retrieved = _before(payload["retrieved_at"], recorded)
        if payload.get("source_published_at") is not None:
            _before(payload["source_published_at"], retrieved)
        evidence.add(_identity(item["id"]))
    if frozen["market"] is not None:
        for event in frozen["market"]["events"]:
            if event["mode"] not in modes:
                raise DataError("Proposal market evidence mode differs from its investigation")
            recorded = _before(event["recorded_at"], deadline)
            retrieved = _before(event["retrieved_at"], recorded)
            if event.get("source_published_at") is not None:
                _before(event["source_published_at"], retrieved)
            if event.get("occurred_at") is not None:
                _before(event["occurred_at"], retrieved)
            evidence.add(_identity(event["record_id"]))
    return evidence


def validate_proposal_sources(output, frozen):
    """Bind V2 citations and sizing to the exact selected snapshot and input cutoff.

    Direct capture references must be present in ``market_captures``. Captures
    referenced only inside evidence remain accessible through that evidence ID;
    omitted records, prior outputs and new files are not silently added as inputs.
    V1 retains its existing artifact-reader reference validation unchanged.
    """
    value = _validated_output(output)
    if "schema_version" not in value:
        return None
    try:
        object_bytes(frozen)
        deadline = _instant(frozen["recorded_at"])
        mode = frozen["request"]["mode"]
        if mode not in {"prospective", "retrospective", "synthetic"}:
            raise DataError("Proposal investigation mode is unsupported")
        modes = {"prospective", "retrospective"} if mode == "retrospective" else {mode}
        known = _evidence_sources(frozen, deadline, modes)
        captures = set()
        for item in frozen["market_captures"]:
            _before(item["capture"]["retrieved_at"], deadline)
            captures.add(_identity(item["id"]))
        cited = set()
        for opportunity in value["opportunities"]:
            cited.update(opportunity["evidence_ids"])
        proposal = value["capital_proposal"]
        if proposal is not None:
            account = frozen["context"]["account"]
            if (
                account is None
                or proposal["snapshot_id"] != account["id"]
                or proposal["snapshot_id"] != frozen["request"]["snapshot_id"]
            ):
                raise DataError("Capital proposal must use the exact selected input snapshot")
            snapshot = account["snapshot"]
            _before(snapshot["collection_started_at"], deadline)
            completed = _before(snapshot["collection_completed_at"], deadline)
            for observation in snapshot["source_observations"]:
                _before(observation["observed_at"], completed)
            for alternative in proposal["alternatives"]:
                for leg in alternative["legs"]:
                    cited.update(leg["evidence_ids"])
                    if not set(leg["capture_ids"]) <= captures:
                        raise DataError("Capital proposal references a capture outside its input")
        if not cited <= known:
            raise DataError("Proposal references evidence outside its frozen input")
    except KeyError, TypeError, ValueError, OverflowError, RecursionError:
        raise DataError("Proposal references or frozen source fields are invalid") from None
    return None
