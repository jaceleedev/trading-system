"""Resolve explicit navigation against saved sources, with no operational effects.

Every call starts from empty selections. A valid object identity alone does not
establish a relationship. In particular, report links use its frozen book export,
never later intents in the current book. Navigation neither creates budgets nor
submits investigations, paper intents, or outcome calculations.
"""

from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trading_research.capital_plans import read_plan
from trading_research.errors import DataError
from trading_research.guided_flow_api import GuidedContext, GuidedFlowResponse, GuidedSelection
from trading_research.investigation_artifacts import read_artifact
from trading_research.jobs import JobStoreUnavailable, workspace_key
from trading_research.outcome_artifacts import read_outcome_from_stores
from trading_research.private_store import get_object, list_objects, object_bytes
from trading_research.serialization import fingerprint
from trading_research.toss_account import public_snapshot

INVALID = (DataError, OSError, KeyError, TypeError, ValueError, ValidationError)
UNAVAILABLE = (JobStoreUnavailable, SQLAlchemyError)


def _require(condition):
    if not condition:
        raise DataError("Selected navigation sources do not share the required lineage")


def _same(left, right):
    return object_bytes({"value": left}) == object_bytes({"value": right})


class GuidedFlowService:
    def __init__(self, workspace, job_store=None, *, synthetic=False):
        self.workspace = Path(workspace)
        self.base = self.workspace / "var"
        self.jobs = job_store
        self.synthetic = synthetic
        self.namespace = workspace_key(self.workspace)
        for path in (
            self.workspace,
            self.base,
            *(
                self.base / name
                for name in ("accounts", "capital-plans", "investigations", "outcomes")
            ),
        ):
            _require(not path.is_symlink() and (not path.exists() or path.is_dir()))
        _require(job_store is None or job_store.workspace_key == self.namespace)

    def _issue(self, code, stage, message):
        issue = {"code": code, "stage": stage, "message": message}
        if issue not in self.issues:
            self.issues.append(issue)

    def _snapshot(self, identity):
        return public_snapshot(get_object(self.base / "accounts", identity))

    def _mode(self, mode):
        _require(not self.synthetic or mode == "synthetic")

    def _artifact(self, identity, kind):
        return read_artifact(self.base / "investigations", identity, expected_kind=kind)

    def _catalog(self, directory, kind, stage):
        identities = list_objects(self.base / directory)
        _require(len(identities) <= 10000)
        entries = []
        for identity in identities:
            try:
                value = get_object(self.base / directory, identity)
                if value.get("kind") == kind:
                    entries.append((identity, value))
            except INVALID:
                self._issue(
                    "invalid_catalog_record",
                    stage,
                    "일부 저장 자료를 검증하지 못해 목록에서 제외했습니다.",
                )
        return sorted(
            entries, key=lambda item: (item[1].get("recorded_at", ""), item[0]), reverse=True
        )

    def _investigation_rows(self, identity, revision):
        from trading_research.models import InvestigationRevisionRow, InvestigationRow

        _require(self.jobs is not None)
        with Session(self.jobs.engine) as session:
            row = session.scalar(
                select(InvestigationRow).where(
                    InvestigationRow.id == identity,
                    InvestigationRow.workspace_key == self.namespace,
                )
            )
            _require(row is not None)
            query = select(InvestigationRevisionRow).where(
                InvestigationRevisionRow.investigation_id == identity,
                InvestigationRevisionRow.workspace_key == self.namespace,
            )
            rows = list(
                session.scalars(query.order_by(InvestigationRevisionRow.number.desc()).limit(100))
            )
            if revision is not None and revision not in {r.number for r in rows}:
                historical = session.scalar(
                    query.where(InvestigationRevisionRow.number == revision)
                )
                if historical is not None:
                    rows.append(historical)
            return row.current_revision, [
                {
                    "number": r.number,
                    "descriptor": r.context_input,
                    "input_sha256": r.input_sha256,
                    "job_id": r.job_id,
                    "result": r.result,
                }
                for r in rows
            ]

    def _revision(self, investigation_id, row):
        descriptor = row["descriptor"]
        input_id = descriptor["input_id"]
        frozen = self._artifact(input_id, "investigation_input")
        _require(row["input_sha256"] == fingerprint(descriptor))
        _require(row["number"] == (frozen["base_revision"] or 0) + 1)
        _require(
            _same(
                descriptor,
                {"input_id": input_id, **frozen["request"], "as_of": frozen["recorded_at"]},
            )
        )
        self._mode(frozen["request"]["mode"])
        output_id, output = None, None
        if row["result"] is not None:
            output_id = row["result"]["output_id"]
            saved = self._artifact(output_id, "investigation_output")
            run = self._artifact(row["result"]["run_id"], "investigation_run")
            _require(saved["input_id"] == input_id)
            _require(
                run["investigation_id"] == investigation_id and run["revision"] == row["number"]
            )
            _require(run["input_id"] == input_id and run["output_id"] == output_id)
            _require(run["job_id"] == row["job_id"] and run["status"] == "process_completed")
            output = saved["output"]
        return (
            {
                "number": row["number"],
                "input_id": input_id,
                "output_id": output_id,
                "snapshot_id": frozen["request"]["snapshot_id"],
                "mode": frozen["request"]["mode"],
            },
            frozen,
            output,
        )

    def _investigation(self):
        request, selection = self.request, self.selection
        if request.investigation_id is None:
            if request.revision is not None or request.output_id is not None:
                self._issue(
                    "investigation_required",
                    "investigations",
                    "먼저 조사를 선택한 뒤 버전과 출력을 선택하세요.",
                )
            return None
        if self.jobs is None:
            self._issue(
                "investigations_unavailable",
                "investigations",
                "조사 연결 확인에는 로컬 작업 저장소가 필요합니다. "
                "저장 계획과 결과는 각 화면에서 읽을 수 있습니다.",
            )
            return None
        current, rows = self._investigation_rows(request.investigation_id, request.revision)
        checked = []
        for row in rows:
            try:
                checked.append(self._revision(request.investigation_id, row))
            except INVALID:
                self._issue(
                    "invalid_revision",
                    "investigations",
                    "출처를 확인할 수 없는 조사 버전은 선택할 수 없습니다.",
                )
        _require(checked)
        selection.investigation_id = request.investigation_id
        detail = {
            "id": request.investigation_id,
            "purpose": checked[0][1]["request"]["purpose"],
            "current_revision": current,
            "revisions": [item[0] for item in checked],
            "output": None,
        }
        selected = next((item for item in checked if item[0]["number"] == request.revision), None)
        if selected is None:
            self._issue(
                "revision_required",
                "investigations",
                "이어갈 조사 버전을 명시적으로 선택하세요.",
            )
            return detail
        summary, frozen, output = selected
        detail["purpose"] = frozen["request"]["purpose"]
        account = frozen["context"]["account"]
        if self.context.account_seq is None or (
            account is not None
            and str(account["snapshot"]["account_seq"]) != self.context.account_seq
        ):
            self._issue(
                "investigation_account_mismatch",
                "investigations",
                "상단에서 이 조사의 계좌를 선택하세요. 현재 계좌 선택은 바꾸지 않았습니다.",
            )
            return detail
        selection.revision = summary["number"]
        self.context.frozen_snapshot_id = summary["snapshot_id"]
        self.context.mode = summary["mode"]
        if request.output_id is None or request.output_id != summary["output_id"]:
            self._issue(
                "output_required",
                "investigations",
                "선택한 버전에 저장된 출력을 선택하세요. 다른 버전의 출력은 연결할 수 없습니다.",
            )
        else:
            selection.output_id = request.output_id
            detail["output"] = output
        return detail

    def _plan_matches(self, plan):
        self._mode(plan["request"]["mode"])
        _require(str(plan["snapshot"]["account_seq"]) == self.context.account_seq)
        if self.request.investigation_id is not None:
            _require(self.selection.output_id is not None)
            _require(
                plan["request"]["source"]
                == {"kind": "investigation_output", "id": self.selection.output_id}
            )
            _require(plan["request"]["snapshot_id"] == self.context.frozen_snapshot_id)
            _require(plan["request"]["mode"] == self.context.mode)
        if self.context.mode is not None:
            _require(plan["request"]["mode"] == self.context.mode)

    def _plan_summary(self, identity, plan):
        return {
            "id": identity,
            "recorded_at": plan["recorded_at"],
            "snapshot_id": plan["request"]["snapshot_id"],
            "mode": plan["request"]["mode"],
            "alternatives": [
                {
                    "key": a["key"],
                    "label": a["label"],
                    "eligibility": a["eligibility"],
                    "currencies": sorted({leg["currency"] for leg in a["legs"]}),
                }
                for a in plan["calculation"]["alternatives"]
            ],
        }

    def _plans(self):
        plans, selected = [], None
        if self.request.investigation_id is None and (
            self.request.revision is not None or self.request.output_id is not None
        ):
            return plans, selected
        if self.context.account_seq is None or (
            self.request.investigation_id is not None and self.selection.output_id is None
        ):
            return plans, selected
        # Resolve a requested identity independently of bounded suggestion catalogs.
        if self.request.plan_id is not None:
            try:
                selected = read_plan(self.workspace, self.request.plan_id)
                self._plan_matches(selected)
            except INVALID:
                selected = None
                self._issue(
                    "plan_lineage_mismatch",
                    "capital",
                    "계획의 출처·계좌·관측·자료 구분가 현재 선택과 맞지 않습니다. "
                    "연결된 계획을 다시 선택하세요.",
                )
        if selected is not None:
            self.selection.plan_id = self.request.plan_id
            self.context.frozen_snapshot_id = selected["request"]["snapshot_id"]
            self.context.mode = selected["request"]["mode"]
        count = 0
        for identity, raw in self._catalog("capital-plans", "capital_plan", "capital"):
            try:
                self._plan_matches(raw)
                count += 1
                if count > 100:
                    continue
                plan = read_plan(self.workspace, identity)
                self._plan_matches(plan)
                plans.append(self._plan_summary(identity, plan))
            except INVALID:
                continue
        if count > 100:
            self._issue(
                "plans_truncated",
                "capital",
                "관련 계획은 최근 100개까지 표시합니다. "
                "기존 계획 화면에서 ID를 선택할 수 있습니다.",
            )
        if selected is not None and not any(p["id"] == self.request.plan_id for p in plans):
            plans.insert(0, self._plan_summary(self.request.plan_id, selected))
        if selected is not None:
            alternative = next(
                (
                    a
                    for a in selected["request"]["alternatives"]
                    if a["key"] == self.request.alternative_id
                ),
                None,
            )
            if alternative is not None:
                self.selection.alternative_id = self.request.alternative_id
                self.context.currencies = sorted({leg["currency"] for leg in alternative["legs"]})
            else:
                self._issue(
                    "alternative_required",
                    "capital",
                    "이 계획에 저장된 대안을 선택하세요. "
                    "예산 반영과 배정은 계획 화면의 실행 버튼으로 진행합니다.",
                )
        elif not plans:
            self._issue(
                "plan_required",
                "capital",
                "연결된 저장 계획이 없습니다. "
                "계획 화면에서 예산과 대안을 입력하고 명시적으로 저장하세요.",
            )
        return plans, selected

    def _book(self, identity, plan):
        from trading_research.models import PaperBookRow, PaperIntentRow
        from trading_research.paper_api import PaperSeed

        _require(self.jobs is not None)
        with Session(self.jobs.engine) as session:
            book = session.scalar(
                select(PaperBookRow).where(
                    PaperBookRow.id == identity, PaperBookRow.workspace_key == self.namespace
                )
            )
            _require(book is not None)
            snapshot = self._snapshot(book.snapshot_id)
            _require(str(snapshot["account_seq"]) == book.account_seq == self.context.account_seq)
            self._mode(book.mode)
            _require(self.context.mode is None or book.mode == self.context.mode)
            seed = book.seed
            _require(_same(PaperSeed.model_validate(seed).model_dump(), seed))
            _require(
                seed["snapshot_id"] == book.snapshot_id
                and seed["account_seq"] == book.account_seq
                and seed["mode"] == book.mode
            )
            expected_holdings = [
                {
                    "market": h["marketCountry"],
                    "symbol": h["symbol"],
                    "currency": h["currency"],
                    "quantity": h["quantity"],
                    "average_purchase_price": h["averagePurchasePrice"],
                }
                for h in snapshot["holdings"]["items"]
            ]
            _require(_same(seed["holdings"], expected_holdings))
            self.book_seeds[identity] = seed
            currencies = {item["currency"] for item in seed["initial_cash"] + seed["holdings"]}
            _require(set(self.context.currencies) <= currencies)
            linked = False
            if plan is not None and self.selection.alternative_id is not None:
                alternative = next(
                    a
                    for a in plan["request"]["alternatives"]
                    if a["key"] == self.selection.alternative_id
                )
                intents = session.scalars(
                    select(PaperIntentRow).where(
                        PaperIntentRow.book_id == identity,
                        PaperIntentRow.workspace_key == self.namespace,
                        PaperIntentRow.plan_id == self.selection.plan_id,
                        PaperIntentRow.alternative_id == self.selection.alternative_id,
                    )
                ).all()
                for intent in intents:
                    _require(intent.account_seq == book.account_seq and intent.mode == book.mode)
                    _require(_same(intent.alternative, alternative))
                    linked = True
            return {
                "id": identity,
                "label": seed["label"],
                "snapshot_id": book.snapshot_id,
                "mode": book.mode,
                "currencies": sorted(currencies),
                "linked": linked,
            }

    def _books(self, plan):
        from trading_research.models import PaperBookRow

        books = []
        if self.request.plan_id is None and self.request.alternative_id is not None:
            self._issue(
                "plan_required",
                "capital",
                "대안이 속한 계획을 먼저 선택하세요. 대안 이름만으로 원장에 연결할 수 없습니다.",
            )
            return books
        if (self.request.output_id is not None or self.request.revision is not None) and (
            self.selection.output_id is None
        ):
            return books
        if self.context.account_seq is None or (
            self.request.plan_id is not None and self.selection.alternative_id is None
        ):
            return books
        if self.request.investigation_id is not None and self.selection.alternative_id is None:
            return books
        if self.jobs is None:
            if self.request.book_id is not None:
                self._issue(
                    "books_unavailable",
                    "paper",
                    "현재 모의 원장 확인에는 로컬 작업 저장소가 필요합니다. "
                    "저장 기간 결과는 결과 화면에서 읽을 수 있습니다.",
                )
            return books
        with Session(self.jobs.engine) as session:
            identities = list(
                session.scalars(
                    select(PaperBookRow.id)
                    .where(
                        PaperBookRow.workspace_key == self.namespace,
                        PaperBookRow.account_seq == self.context.account_seq,
                    )
                    .order_by(PaperBookRow.created_at.desc())
                    .limit(101)
                )
            )
        if len(identities) > 100:
            self._issue(
                "books_truncated",
                "paper",
                "모의 원장은 최근 100개까지 표시합니다. "
                "기존 원장 화면에서 ID를 선택할 수 있습니다.",
            )
        identities = list(
            dict.fromkeys(
                ([self.request.book_id] if self.request.book_id else []) + identities[:100]
            )
        )
        for identity in identities:
            try:
                book = self._book(identity, plan)
                books.append(book)
                if identity == self.request.book_id:
                    self.selection.book_id = identity
                    if self.context.mode is None:
                        self.context.mode = book["mode"]
                    if self.context.frozen_snapshot_id is None:
                        self.context.frozen_snapshot_id = book["snapshot_id"]
                    if plan is not None and not book["linked"]:
                        self._issue(
                            "paper_submission_required",
                            "paper",
                            "이 원장은 호환되지만 계획·대안을 아직 모의 선택하지 않았습니다. "
                            "원장 화면에서 가정과 모의 선택 버튼을 확인하세요.",
                        )
            except INVALID:
                if identity == self.request.book_id:
                    self._issue(
                        "book_lineage_mismatch",
                        "paper",
                        "원장의 계좌·자료 구분·통화 또는 저장 출처가 맞지 않습니다. "
                        "호환되는 원장을 다시 선택하세요.",
                    )
        if not books and plan is not None:
            self._issue(
                "book_required",
                "paper",
                "호환되는 모의 원장이 없습니다. 원장 화면에서 시작 금액을 직접 입력해 만드세요.",
            )
        return books

    def _report_matches(self, identity):
        report = read_outcome_from_stores(self.base, identity)
        _require(report["kind"] == "outcome_report")
        self._mode(report["mode"])
        _require(self.context.mode is None or report["mode"] == self.context.mode)
        frozen = read_outcome_from_stores(self.base, report["input_id"])
        # The report already validates its exact exported source and calculation.
        # Check *all* accounts before attaching it to the explicit account selection.
        sources = frozen["source"]
        accounts = {book["account_seq"] for book in sources["books"]}
        accounts.update(item["workflow"]["account_seq"] for item in sources["workflows"])
        _require(accounts == {self.context.account_seq})
        if self.request.book_id is not None:
            _require(self.selection.book_id is not None)
            book = next(
                (b for b in sources["books"] if b["book_id"] == self.selection.book_id), None
            )
            _require(book is not None)
            _require(_same(book["seed"], self.book_seeds[self.selection.book_id]))
            if self.request.plan_id is not None:
                _require(self.selection.alternative_id is not None)
                _require(
                    any(
                        i["plan_id"] == self.selection.plan_id
                        and i["alternative_id"] == self.selection.alternative_id
                        for i in book["intents"]
                    )
                )
        elif self.request.plan_id is not None or self.request.investigation_id is not None:
            raise DataError("Choose a paper book before following this plan to a report")
        return {
            "id": identity,
            **{key: report[key] for key in ("start_at", "end_at", "recorded_at", "mode")},
        }

    def _reports(self):
        reports = []
        if self.request.alternative_id is not None and self.selection.alternative_id is None:
            return reports
        if (self.request.output_id is not None or self.request.revision is not None) and (
            self.selection.output_id is None
        ):
            return reports
        if self.context.account_seq is None or (
            self.request.book_id is not None and self.selection.book_id is None
        ):
            return reports
        if (
            self.request.plan_id is not None or self.request.investigation_id is not None
        ) and self.selection.book_id is None:
            return reports
        if self.request.report_id is not None:
            try:
                report = self._report_matches(self.request.report_id)
                reports.append(report)
                self.selection.report_id = self.request.report_id
                if self.context.mode is None:
                    self.context.mode = report["mode"]
            except INVALID:
                self._issue(
                    "report_lineage_mismatch",
                    "outcomes",
                    "보고서의 고정된 기간 자료에 선택한 원장·계획·대안이 연결되어 있지 않습니다. "
                    "해당 기간의 결과를 다시 선택하세요.",
                )
        count = 0
        for identity, _ in self._catalog("outcomes", "outcome_report", "outcomes"):
            if identity == self.request.report_id:
                continue
            count += 1
            if count > 100:
                continue
            try:
                reports.append(self._report_matches(identity))
            except INVALID:
                continue
        if count > 100:
            self._issue(
                "reports_truncated",
                "outcomes",
                "저장 결과는 최근 100개까지 확인합니다. "
                "기존 결과 화면에서 ID를 선택할 수 있습니다.",
            )
        if self.selection.book_id is not None and not reports:
            self._issue(
                "report_required",
                "outcomes",
                "이 선택과 연결된 기간 결과가 없습니다. "
                "결과 화면에서 원장과 기간을 확인한 뒤 계산 버튼을 누르세요.",
            )
        return reports

    def resolve(self, selection):
        self.request = GuidedSelection.model_validate(selection)
        self.selection = GuidedSelection()
        self.context = GuidedContext()
        self.book_seeds = {}
        self.issues = []
        jobs_available = self.jobs is not None
        if self.request.snapshot_id is not None:
            try:
                snapshot = self._snapshot(self.request.snapshot_id)
                self.selection.snapshot_id = self.request.snapshot_id
                self.context.account_seq = str(snapshot["account_seq"])
            except INVALID:
                self._issue(
                    "snapshot_invalid",
                    "investigations",
                    "선택한 계좌 관측을 확인할 수 없습니다. 상단에서 저장 관측을 다시 선택하세요.",
                )
        else:
            self._issue(
                "account_required",
                "investigations",
                "상단에서 계좌 관측을 먼저 선택하세요. "
                "과거 탐색이 계좌 선택을 자동으로 바꾸지 않습니다.",
            )
        investigation = None
        try:
            investigation = self._investigation()
        except UNAVAILABLE:
            jobs_available = False
            self._issue(
                "investigations_unavailable",
                "investigations",
                "조사 작업 저장소에 연결할 수 없습니다. 선택의 출처를 다시 확인한 뒤 이어가세요.",
            )
        except INVALID:
            self._issue(
                "investigation_invalid",
                "investigations",
                "조사 또는 저장 출처를 확인할 수 없습니다. 조사를 다시 선택하세요.",
            )
        plans, plan = self._plans()
        books = []
        try:
            books = self._books(plan)
        except UNAVAILABLE:
            jobs_available = False
            self._issue(
                "books_unavailable",
                "paper",
                "모의 원장 저장소에 연결할 수 없습니다. 복구 후 선택을 다시 확인하세요.",
            )
        reports = self._reports()
        if (
            self.context.frozen_snapshot_id is not None
            and self.context.frozen_snapshot_id != self.selection.snapshot_id
        ):
            self._issue(
                "frozen_snapshot_preserved",
                "investigations",
                "상단 계좌 관측과 과거 입력 관측이 다릅니다. "
                "과거 조사·계획에 고정된 관측을 그대로 유지합니다.",
            )
        return GuidedFlowResponse(
            workspace_key=self.namespace,
            selection=self.selection,
            context=self.context,
            investigation=investigation,
            plans=plans,
            books=books,
            reports=reports,
            issues=self.issues,
            jobs_available=jobs_available,
            orders_enabled=False,
        )
