from __future__ import annotations

import argparse
import json
from pathlib import Path

from .identity import repository_identity
from .memory import InterventionMemory, MemoryIntegrityError, utc_now


def _memory(args: argparse.Namespace) -> tuple[InterventionMemory, str]:
    _, repo_id = repository_identity(args.repo)
    return InterventionMemory(args.db, repo_id), repo_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comeback")
    parser.add_argument("--db", required=True)
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    intervene = sub.add_parser("intervene")
    intervene.add_argument("--record", required=True, help="Signed intervention JSON")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--session-id")
    inspect.add_argument("--task-class", default="release")
    inspect.add_argument("--area", default="release_workflow")
    inspect.add_argument("--agent-family", default="Codex")

    approve = sub.add_parser("approve")
    approve.add_argument("--session-id", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.add_argument("--signature", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        memory, repo_id = _memory(args)
        if args.command == "intervene":
            record = json.loads(args.record)
            result = memory.record_intervention(record)
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

