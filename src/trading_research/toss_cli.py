"""Interactive credential setup and explicit authentication, without secret output."""

import getpass
import json
import sys
import warnings

from trading_research.errors import DataError


def add_auth_parser(subparsers):
    parser = subparsers.add_parser("toss-auth", help="Configure or check local Toss authentication")
    parser.add_argument("action", choices=["configure", "status", "check"])


def handle_auth(action: str) -> dict:
    from trading_research.credentials import credential_status, save_credentials
    from trading_research.toss_auth import resolve_access_token

    if action == "status":
        return credential_status()
    if action == "configure":
        if not sys.stdin.isatty():
            raise DataError("Run toss-auth configure directly in an interactive local terminal")
        # Refuse getpass's echoed-input fallback, including detached terminals.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                client_id = getpass.getpass("Toss Client ID (hidden): ")
                client_secret = getpass.getpass("Toss Client Secret (hidden): ")
        except getpass.GetPassWarning, EOFError, KeyboardInterrupt:
            raise DataError("Credential input canceled; no credentials saved") from None
        save_credentials(client_id, client_secret)
        return {"status": "configured", "storage": "macos-keychain", "orders_enabled": False}
    if action == "check":
        resolve_access_token()
        return {
            "status": "token_available",
            "account_connection_verified": False,
            "orders_enabled": False,
        }
    raise DataError("Unknown authentication action")


def print_auth(action: str) -> None:
    print(json.dumps(handle_auth(action), ensure_ascii=False, indent=2))
