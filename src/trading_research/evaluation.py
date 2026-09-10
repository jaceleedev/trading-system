"""Fixed retrospective evaluation protocol without tuning or winner selection."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from trading_research.backtest import BacktestConfig, run_backtest
from trading_research.data import Bundle, calendar_date, decimal_value
from trading_research.errors import DataError
from trading_research.numeric import research_arithmetic
from trading_research.serialization import encode, fingerprint
from trading_research.strategy import ResearchConfig

_WINDOW_NAMES = ("development", "validation", "reserved")
_METRICS = (
    "ending_nav_krw",
    "external_net_krw",
    "net_pnl_krw",
    "time_weighted_return",
    "annualized_twr",
    "max_drawdown_twr",
    "modeled_cost_krw",
    "fills",
)


@dataclass(frozen=True)
class EvaluationWindow:
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class EvaluationPlan:
    label: str
    windows: tuple[EvaluationWindow, ...]
    initial_cash_krw: Decimal
    monthly_contribution_krw: Decimal
    contribution_day: int
    benchmarks: dict[str, str]
    cost_multiples: tuple[int, ...]

    @classmethod
    def load(cls, path: str | Path) -> EvaluationPlan:
        try:
            raw = json.loads(Path(path).read_text(), object_pairs_hook=_unique_object)
        except OSError, ValueError, UnicodeError, RecursionError:
            raise DataError(
                "Evaluation plan could not be read as an unambiguous JSON object"
            ) from None
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw: dict) -> EvaluationPlan:
        if type(raw) is not dict or set(raw) != set(cls.__dataclass_fields__):
            raise DataError("Evaluation plan has unknown or missing fields")
        label = raw["label"]
        if not isinstance(label, str) or not label.strip():
            raise DataError("Evaluation plan requires a nonempty label")
        raw_windows = raw["windows"]
        if type(raw_windows) is not list or len(raw_windows) != 3:
            raise DataError("Evaluation requires exactly three ordered windows")
        windows = []
        for expected_name, item in zip(_WINDOW_NAMES, raw_windows, strict=True):
            if type(item) is not dict or set(item) != {"name", "start", "end"}:
                raise DataError("Evaluation windows require exactly name, start, and end")
            if item["name"] != expected_name:
                raise DataError("Evaluation window order must be development, validation, reserved")
            if type(item["start"]) is not str or type(item["end"]) is not str:
                raise DataError("Evaluation window dates must be ISO date strings")
            start, end = calendar_date(item["start"]), calendar_date(item["end"])
            if not start < end:
                raise DataError("Every evaluation window must start before it ends")
            # Backtest endpoints are inclusive: sharing an endpoint is an overlap.
            if windows and start <= windows[-1].end:
                raise DataError("Evaluation windows must be chronological and non-overlapping")
            windows.append(EvaluationWindow(expected_name, start, end))
        multiples = raw["cost_multiples"]
        if (
            type(multiples) is not list
            or any(type(value) is not int for value in multiples)
            or multiples != [1, 2, 3]
        ):
            raise DataError("This evaluation protocol requires cost_multiples [1, 2, 3]")
        day = raw["contribution_day"]
        if type(day) is not int or not 1 <= day <= 28:
            raise DataError("Contribution day must be 1 through 28")
        benchmarks = raw["benchmarks"]
        if type(benchmarks) is not dict or set(benchmarks) != {"KR", "US"}:
            raise DataError("Evaluation requires explicit KR and US reference instrument IDs")
        if any(not isinstance(value, str) or not value.strip() for value in benchmarks.values()):
            raise DataError("Reference instrument IDs must be nonempty strings")
        return cls(
            label.strip(),
            tuple(windows),
            decimal_value(str(raw["initial_cash_krw"])),
            decimal_value(str(raw["monthly_contribution_krw"]), positive=False),
            day,
            {market: identifier.strip() for market, identifier in benchmarks.items()},
            tuple(multiples),
        )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Evaluation plan contains duplicate JSON fields")
        result[key] = value
    return result


def _source_sha256() -> str:
    return fingerprint({"evaluation.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


@research_arithmetic
def evaluate(
    bundle: Bundle, strategy: ResearchConfig, plan: EvaluationPlan
) -> tuple[dict, list[dict]]:
    """Return every window/cost case, or raise without returning partial success.

    Each backtest starts fresh. The complete input dataset remains available for
    historical signal warm-up; the backtest enforces decision-time visibility.
    This function neither persists results nor chooses or changes a strategy.
    """
    if not isinstance(strategy, ResearchConfig) or not isinstance(plan, EvaluationPlan):
        raise DataError("Evaluation requires a research configuration and an evaluation plan")
    # Detach mutable mappings and validate even directly constructed dataclasses.
    strategy_raw = json.loads(encode(asdict(strategy)))
    baseline = ResearchConfig.parse(strategy_raw)
    plan = EvaluationPlan.parse(json.loads(encode(asdict(plan))))
    costs = []
    for multiple in plan.cost_multiples:
        candidate = asdict(baseline)
        for field in ("buy_cost_bps", "sell_cost_bps"):
            candidate[field] = {
                market: amount * multiple for market, amount in candidate[field].items()
            }
        # Includes the existing ResearchConfig cost ceiling for every stressed case.
        costs.append((multiple, ResearchConfig.parse(json.loads(encode(candidate)))))
    simulations = []
    for window in plan.windows:
        simulations.append(
            BacktestConfig.parse(
                {
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                    "initial_cash_krw": str(plan.initial_cash_krw),
                    "monthly_contribution_krw": str(plan.monthly_contribution_krw),
                    "contribution_day": plan.contribution_day,
                    "benchmarks": dict(plan.benchmarks),
                }
            )
        )

    source_sha256 = _source_sha256()
    payloads, cases = [], []
    warnings = bundle.evidence_warnings() + [
        "Retrospective evaluation: the reserved window is not proven unseen "
        "or prospectively held out",
        "All nine cases are retained; no parameter selection, selected winner, or profit claim",
        "Each window resets positions, cash, and receivables to a fresh hypothetical account",
        "Separate window results must not be compounded into one continuous portfolio return",
        "Cost multiples are assumptions; multiplying a zero baseline cost leaves it zero",
    ]
    for window, simulation in zip(plan.windows, simulations, strict=True):
        for multiple, stressed in costs:
            # The simulator creates new books on every call. No state crosses a window or case.
            payload = run_backtest(bundle, stressed, simulation)
            payloads.append(payload)
            cases.append(
                {
                    "window": window.name,
                    "cost_multiple": multiple,
                    "backtest_id": payload["id"],
                    "payload_sha256": fingerprint(payload),
                    "results": {
                        name: {metric: result[metric] for metric in _METRICS}
                        for name, result in payload["results"].items()
                    },
                }
            )
            warnings.extend(payload["warnings"])
    identity = {
        "dataset_id": bundle.id,
        "dataset_sha256": bundle.sha256,
        "dataset_content_sha256": bundle.content_sha256,
        "config": asdict(baseline),
        "plan": asdict(plan),
        "source_sha256": source_sha256,
        "backtest_ids": [case["backtest_id"] for case in cases],
        "backtest_payload_sha256": [case["payload_sha256"] for case in cases],
        "evidence_level": (
            "synthetic_pipeline_validation"
            if bundle.manifest["kind"] == "synthetic"
            else "retrospective_research"
        ),
        "live_recommendation_ready": False,
        "edge_status": "not_established",
    }
    summary = {
        **identity,
        "id": fingerprint(identity),
        "kind": "research_evaluation",
        "cases": cases,
        "warnings": list(dict.fromkeys(warnings)),
        "orders_enabled": False,
    }
    return json.loads(encode(summary)), payloads
