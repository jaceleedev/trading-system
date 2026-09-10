"""Interactive credential setup and explicit authentication, without secret output."""

import getpass
import json
import sys
import warnings
from pathlib import Path

from trading_research.errors import DataError


def add_auth_parser(subparsers):
    parser = subparsers.add_parser("toss-auth", help="Configure or check local Toss authentication")
    parser.add_argument("action", choices=["configure", "status", "check"])


def add_account_parser(subparsers):
    parser = subparsers.add_parser(
        "toss-account", help="Read and preserve Toss account observations"
    )
    parser.add_argument("action", choices=["list", "sync", "show"])
    parser.add_argument("--account", type=int, help="Explicit account_seq from toss-account list")
    parser.add_argument("--id", help="Stored snapshot ID for offline show")
    parser.add_argument("--store", default="var/accounts", help="Private local object directory")


def handle_account(args) -> dict:
    from trading_research.private_store import get_object, put_object
    from trading_research.toss_account import TossAccountClient, public_accounts, public_snapshot
    from trading_research.toss_auth import resolve_access_token

    root = Path(args.store)
    if args.action == "show":
        if not args.id:
            raise DataError("toss-account show requires --id")
        result = public_snapshot(get_object(root, args.id))
        return {"id": args.id, "snapshot": result, "orders_enabled": False}
    if args.action == "sync" and args.account is None:
        raise DataError("toss-account sync requires an explicit --account")
    client = TossAccountClient(resolve_access_token())
    if args.action == "list":
        observation = client.accounts()
        accounts = public_accounts(observation)
        identity = put_object(root, observation)
        return {"accounts": accounts, "observation_id": identity, "orders_enabled": False}
    observation_ids = []

    def preserve(observation):
        observation_ids.append(put_object(root, observation))

    snapshot = client.snapshot(args.account, on_observation=preserve)
    projection = public_snapshot(snapshot)
    identity = put_object(root, snapshot)
    return {
        "status": "captured",
        "id": identity,
        "observation_ids": observation_ids,
        "snapshot": projection,
        "orders_enabled": False,
    }


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
