"""Local artifact backup commands with bounded, metadata-only output."""

from pathlib import Path


def add_artifact_backup_parser(subparsers):
    parser = subparsers.add_parser(
        "artifact-backup", help="Back up and verify private local artifact stores; no credentials"
    )
    actions = parser.add_subparsers(dest="artifact_backup_action", required=True)
    create = actions.add_parser(
        "create", help="Copy selected artifacts into a new private directory"
    )
    create.add_argument("--source", default="var")
    create.add_argument("--destination", required=True)
    verify = actions.add_parser("verify", help="Check identities, schemas, and references offline")
    verify.add_argument("--backup", required=True)
    restore = actions.add_parser("restore", help="Restore and verify a new independent directory")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)


def handle_artifact_backup(args):
    from trading_research.artifact_backup import create_backup, restore_backup, verify_backup

    if args.artifact_backup_action == "create":
        return create_backup(Path(args.source), Path(args.destination))
    if args.artifact_backup_action == "verify":
        return verify_backup(Path(args.backup))
    return restore_backup(Path(args.backup), Path(args.destination))
