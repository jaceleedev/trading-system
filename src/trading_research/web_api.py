"""Local read-only HTTP interface to the existing investment workspace services."""

import argparse
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from trading_research import dashboard_investment_service as service
from trading_research import decision_context
from trading_research.api_models import (
    AccountSnapshotsResponse,
    ErrorResponse,
    HealthResponse,
    InvestmentContext,
    ObjectId,
    ResearchResponse,
)
from trading_research.errors import DataError

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
STATIC_SUFFIXES = {
    ".html",
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
}
PRIVATE_PREFIXES = {"api", "var", "src", "tests", "docs", "scripts", "configs", "web"}


def _error(status, code, message):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _origin(value):
    """Parse an origin, rejecting credentials and URL components beyond authority."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class LocalBrowserBoundary:
    """Reject foreign browser origins before reading private records; contain every error."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [v.decode("latin1") for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
        sites = [v.decode("latin1") for k, v in headers if k.lower() == b"sec-fetch-site"]
        expected = _origin(f"{scope['scheme']}://{hosts[0]}") if len(hosts) == 1 else None
        started = False

        async def private_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {b"cache-control", b"pragma", b"expires"}
                ] + [
                    (b"cache-control", b"no-store"),
                    (b"pragma", b"no-cache"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
            await send(message)

        if expected is None:
            response = _error(400, "invalid_host", "Only a loopback host is supported.")
        elif (
            len(origins) > 1
            or (origins and _origin(origins[0]) != expected)
            or len(sites) > 1
            or (sites and sites[0] not in {"same-origin", "none"})
        ):
            response = _error(403, "foreign_origin", "Open the workspace from its local address.")
        else:
            try:
                await self.app(scope, receive, private_send)
                return
            except Exception:
                # Do not pass exception text/tracebacks (including response validation inputs)
                # to Uvicorn or clients. Streaming file failures cannot replace sent headers.
                if started:
                    return
                response = _error(500, "internal_error", "The local workspace request failed.")
        await response(scope, receive, private_send)


def _check_roots(workspace):
    for path in (
        workspace,
        workspace / "var",
        workspace / "var/accounts",
        workspace / "var/research",
        workspace / "var/captures",
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DataError("Workspace stores are unavailable or unsafe")


def _require_object(root, identity):
    path = root / f"{identity}.json"
    if not path.exists() and not path.is_symlink():
        raise HTTPException(404)


def _static_file(root, relative):
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or "\\" in relative:
        return None
    if any(part.startswith(".") or part in {"", ".", ".."} for part in path.parts):
        return None
    allowed = path.suffix in STATIC_SUFFIXES or relative == "_app/version.json"
    if not allowed:
        return None
    candidate = root
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            return None
    if root.is_symlink() or not candidate.is_file():
        return None
    if not candidate.resolve().is_relative_to(root.resolve()):
        return None
    return candidate


def create_app(
    workspace: Path | None = None,
    static_dir: Path | None = None,
    *,
    synthetic=False,
    job_store=None,
):
    """Bind file roots once; initialization performs no account, auth, network or DB reads."""
    workspace = Path.cwd() if workspace is None else Path(workspace).absolute()
    static_dir = workspace / "web/build" if static_dir is None else Path(static_dir).absolute()
    if static_dir == workspace or static_dir.resolve().is_relative_to(
        (workspace / "var").resolve()
    ):
        raise DataError("Static files must use a separate web build directory")
    accounts, research = workspace / "var/accounts", workspace / "var/research"
    app = FastAPI(
        title="Investment workspace API",
        version="1.0.0",
        description=(
            "Local research, capital plans and opted-in jobs. Brokerage orders are disabled."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    # Starlette splits Host at the first colon; '[' is its IPv6 loopback fragment.
    # The outer boundary validates the complete authority before TrustedHost runs.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "["])
    app.add_middleware(LocalBrowserBoundary)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return _error(422, "invalid_request", "Request parameters are invalid.")

    @app.exception_handler(DataError)
    async def invalid_record(_request: Request, _exc: DataError):
        return _error(409, "invalid_record", "Saved records are unavailable or failed validation.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        code = (
            "not_found"
            if exc.status_code == 404
            else "method_not_allowed"
            if exc.status_code == 405
            else "request_failed"
        )
        return _error(exc.status_code, code, "The requested resource is unavailable.")

    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500)}

    @app.get(
        "/api/v1/health", response_model=HealthResponse, operation_id="health", responses=responses
    )
    def health():
        return {
            "status": "ok",
            "service": "trading-investment-web",
            "read_only": job_store is None,
            "orders_enabled": False,
            "synthetic": synthetic,
            "jobs_enabled": job_store is not None,
        }

    @app.get(
        "/api/v1/account-snapshots",
        response_model=AccountSnapshotsResponse,
        response_model_exclude_unset=True,
        operation_id="list_account_snapshots",
        responses=responses,
    )
    def snapshots():
        _check_roots(workspace)
        return {"items": service.list_snapshots(accounts)}

    @app.get(
        "/api/v1/context",
        response_model=InvestmentContext,
        response_model_exclude_unset=True,
        operation_id="get_context",
        responses=responses,
    )
    def context(
        snapshot_id: ObjectId | None = None,
        max_records: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        _check_roots(workspace)
        if snapshot_id is not None:
            _require_object(accounts, snapshot_id)
        return decision_context.build_context(
            research,
            account_root=accounts,
            capture_root=workspace / "var/captures",
            snapshot_id=snapshot_id,
            max_records=max_records,
            now=service.utc_now(),
        )

    @app.get(
        "/api/v1/research/{id}",
        response_model=ResearchResponse,
        response_model_exclude_unset=True,
        operation_id="get_research",
        responses=responses,
    )
    def research_record(id: ObjectId):
        _check_roots(workspace)
        _require_object(research, id)
        return service.load_record(id, research_root=research, account_root=accounts)

    from trading_research.job_api import register_job_routes

    register_job_routes(app, job_store)
    from trading_research.market_api import register_market_routes

    register_market_routes(app, workspace)
    from trading_research.investigation_api import register_investigation_routes

    register_investigation_routes(app, workspace, job_store, synthetic=synthetic)
    from trading_research.capital_api import register_capital_routes

    register_capital_routes(app, workspace, job_store, synthetic=synthetic)
    from trading_research.paper_api import register_paper_routes

    register_paper_routes(app, workspace, job_store, synthetic=synthetic)
    from trading_research.broker_api import register_broker_routes

    register_broker_routes(app, workspace, job_store, synthetic=synthetic)

    @app.get("/{path:path}", include_in_schema=False)
    def static(path: str):
        relative = PurePosixPath(path)
        if relative.parts and (
            relative.parts[0] in PRIVATE_PREFIXES
            or any(part.startswith(".") for part in relative.parts)
            or "\\" in path
        ):
            raise HTTPException(404)
        asset = _static_file(static_dir, path or "index.html")
        if asset is not None:
            return FileResponse(asset)
        if relative.suffix:
            raise HTTPException(404)
        index = _static_file(static_dir, "index.html") or _static_file(static_dir, "200.html")
        if index is None:
            raise HTTPException(404)
        return FileResponse(index)

    return app


def main():
    parser = argparse.ArgumentParser(description="Serve the local read-only investment workspace")
    parser.add_argument("--host", choices=sorted(LOOPBACK_HOSTS), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--static-dir", type=Path)
    parser.add_argument(
        "--jobs", action="store_true", help="Enable local PostgreSQL job submission and monitoring"
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Label this operator-selected workspace as a synthetic demonstration",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be from 1 through 65535")
    import uvicorn

    job_store = None
    if args.jobs:
        from trading_research.jobs import local_job_store

        job_store = local_job_store(args.workspace)
    try:
        uvicorn.run(
            create_app(
                args.workspace, args.static_dir, synthetic=args.synthetic, job_store=job_store
            ),
            host=args.host,
            port=args.port,
            proxy_headers=False,
            access_log=False,
        )
    finally:
        if job_store is not None:
            job_store.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
