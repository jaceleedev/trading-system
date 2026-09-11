"""Offline projections and deterministic reconciliation records for web and CLI."""

from pathlib import Path

from trading_research.errors import DataError


class BrokerService:
    def __init__(self, workspace, *, synthetic=False):
        self.workspace = Path(workspace)
        self.synthetic = synthetic
        for path in (
            self.workspace,
            self.workspace / "var",
            *(
                self.workspace / "var" / name
                for name in (
                    "accounts",
                    "broker-observations",
                    "reconciliations",
                )
            ),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Broker workspace stores are unavailable or unsafe")

    def _mode(self, document):
        mode = document.get("mode") if type(document) is dict else None
        if (self.synthetic and mode != "synthetic") or (not self.synthetic and mode == "synthetic"):
            raise DataError("Reconciliation mode must match the explicit workspace")

    def scans(self, account_seq=None, limit=50):
        from trading_research.broker_artifacts import catalog

        return catalog(
            self.workspace / "var/broker-observations", account_seq=account_seq, limit=limit
        )

    def scan(self, identity):
        from trading_research.broker_artifacts import read_scan

        return read_scan(self.workspace / "var/broker-observations", identity)

    def preview(self, document):
        from trading_research.reconciliation import calculate_report

        self._mode(document)
        return calculate_report(self.workspace, document)

    def save(self, document):
        from trading_research.reconciliation import save_report

        self._mode(document)
        return save_report(self.workspace, document)

    def list(self, limit=50):
        from trading_research.reconciliation import list_reports

        return list_reports(self.workspace, limit=limit)

    def get(self, identity):
        from trading_research.reconciliation import read_report

        return {"id": identity, "record": read_report(self.workspace, identity)}
