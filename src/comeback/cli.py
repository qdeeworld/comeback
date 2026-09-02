from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

from .identity import repository_identity
from .installer import install_repository
from .memory import InterventionMemory, MemoryIntegrityError, utc_now
from .signing import approval_message, intervention_message


def _memory(args: argparse.Namespace) -> tuple[InterventionMemory, str]:
    root, repo_id = repository_identity(args.repo)
    db = Path(args.db).expanduser().resolve() if args.db else root / ".comeback" / "memory.db"
    return InterventionMemory(db, repo_id), repo_id


def _intervention_record(args: argparse.Namespace) -> dict:
    if args.record_file:
        value = json.loads(Path(args.record_file).read_text(encoding="utf-8"))
    elif args.record:
        value = json.loads(args.record)
    else:
        raise MemoryIntegrityError("--record or --record-file is required")
    if not isinstance(value, dict):
        raise MemoryIntegrityError("intervention record must be an object")
    if "record_template" in value:
        value = value["record_template"]
    if args.signature:
        value["intervention_signature"] = args.signature
    if value.get("intervention_signature") in (None, "", "<SIGNATURE>"):
        raise MemoryIntegrityError("the authorized closer's intervention signature is required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comeback")
    parser.add_argument("--db")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("status")

    prepare = sub.add_parser("prepare-intervention")
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument("--authorized-closer", required=True)
    prepare.add_argument("--summary", required=True)
    prepare.add_argument(
        "--agent-scope",
        choices=("same_agent", "all_supported"),
        default="all_supported",
        help="Apply the intervention only to its source agent or to every supported coding agent",
    )
    prepare.add_argument("--severity", default="release_blocker")
    prepare.add_argument(
        "--checkpoint-command",
        required=True,
        help="Exact repository command whose successful result satisfies the remembered checkpoint",
    )

    intervene = sub.add_parser("intervene")
    source = intervene.add_mutually_exclusive_group(required=True)
    source.add_argument("--record", help="Signed intervention JSON")
    source.add_argument("--record-file", help="Prepared intervention JSON file")
    intervene.add_argument("--signature", help="Signature to insert into a prepared record")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--session-id")
    inspect.add_argument("--task-class", default="release")
    inspect.add_argument("--area", default="release_workflow")
    inspect.add_argument("--agent-family", default="Codex")

    approval_message_parser = sub.add_parser("prepare-approval")
    approval_message_parser.add_argument("--session-id", required=True)
    approval_message_parser.add_argument("--approved-at")

    approve = sub.add_parser("approve")
    approve.add_argument("--session-id", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.add_argument("--signature", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "init":
            print(json.dumps(install_repository(args.repo), indent=2, sort_keys=True))
            return
        memory, repo_id = _memory(args)
        if args.command == "status":
            result = {
                "repo_id": repo_id,
                "runs": memory.list_runs(),
                "lessons": memory.matching_lessons("release", "release_workflow", "Codex"),
                "inspected_at": utc_now(),
            }
        elif args.command == "prepare-intervention":
            closer = args.authorized_closer.lower()
            if not closer.startswith("0x") or len(closer) != 42:
                raise MemoryIntegrityError("authorized closer must be an Ethereum address")
            source_session_id = args.session_id
            source_run = memory.get_run(source_session_id)
            task_class = source_run["task_class"]
            area = source_run["area"]
            agent_family = source_run["agent_family"]
            signed_fields = {
                "lesson_id": f"{task_class}-{area}-{agent_family.lower()}",
                "repo_id": repo_id,
                "task_class": task_class,
                "area": area,
                "agent_family": agent_family,
                "agent_scope": args.agent_scope,
                "severity": args.severity,
                "checkpoint_command": args.checkpoint_command.strip(),
                "checkpoint_success_marker": "COMEBACK_CHECK_OK_" + secrets.token_hex(16),
                "release_success_marker": "COMEBACK_RELEASE_OK_" + secrets.token_hex(16),
                "required_evidence": ["release_check_passed", "human_approval"],
                "authorized_closer": closer,
                "source_session_id": source_session_id,
                "incident_at": utc_now(),
            }
            result = {
                "message_to_sign": intervention_message(signed_fields),
                "record_template": {
                    "signed_fields": signed_fields,
                    "intervention_signature": "<SIGNATURE>",
                    "incident_summary": args.summary,
                },
            }
        elif args.command == "intervene":
            result = memory.record_intervention(_intervention_record(args))
        elif args.command == "prepare-approval":
            approval_session_id = args.session_id
            run = memory.get_run(approval_session_id)
            approved_at = args.approved_at or utc_now()
            result = {
                "approved_at": approved_at,
                "message_to_sign": approval_message(run, approved_at),
                "session_id": approval_session_id,
                "remaining": memory.missing_requirements(run),
            }
        elif args.command == "approve":
            result = memory.approve(
                args.session_id, approved_at=args.approved_at, signature=args.signature
            )
        elif args.session_id:
            result = memory.get_run(args.session_id)
        else:
            result = {
                "repo_id": repo_id,
                "lessons": memory.matching_lessons(
                    args.task_class, args.area, args.agent_family
                ),
                "inspected_at": utc_now(),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
    except (MemoryIntegrityError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"decision": "refuse", "reason": str(exc)}, sort_keys=True))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
