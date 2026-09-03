from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

from .diagnostics import diagnose_repository
from .execution import (
    execute_checkpoint,
    execute_release,
    read_release_lock,
    reconcile_release,
)
from .identity import repository_identity
from .installer import install_repository
from .memory import InterventionMemory, MemoryIntegrityError, utc_now
from .owner import (
    create_owner_interactive,
    default_keystore,
    owner_address,
    sign_with_owner,
)
from .signing import approval_message, intervention_message, reconciliation_message


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
    return value


def _keystore_path(args: argparse.Namespace, root: Path) -> Path:
    configured = getattr(args, "keystore", None)
    return Path(configured).expanduser().resolve() if configured else default_keystore(root)


def _command_argv(
    value: str,
    field: str,
    *,
    windows: bool | None = None,
) -> list[str]:
    use_windows_rules = sys.platform == "win32" if windows is None else windows
    if use_windows_rules:
        raise MemoryIntegrityError(
            f"{field} text parsing is ambiguous on Windows; use the corresponding --*-argv-json option"
        )
    try:
        argv = shlex.split(value, posix=True)
    except ValueError as exc:
        raise MemoryIntegrityError(f"{field} has invalid quoting") from exc
    if not argv:
        raise MemoryIntegrityError(f"{field} cannot be empty")
    forbidden = {"&&", "||", ";", "|", ">", ">>", "<"}
    if any(argument in forbidden for argument in argv):
        raise MemoryIntegrityError(
            f"{field} must be one executable plus arguments, not a shell expression"
        )
    return argv


def _json_argv(value: str, field: str) -> list[str]:
    try:
        argv = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MemoryIntegrityError(f"{field} JSON is invalid") from exc
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(argument, str) and argument for argument in argv)
    ):
        raise MemoryIntegrityError(f"{field} JSON must be a non-empty string array")
    return list(argv)


def _prepared_argv(
    command: str | None,
    encoded: str | None,
    field: str,
) -> list[str]:
    if encoded is not None:
        return _json_argv(encoded, field)
    if command is None:
        raise MemoryIntegrityError(f"{field} is required")
    return _command_argv(command, field)


def _confirm_signature(label: str, review: dict) -> None:
    print(f"\nComeback {label.lower()} review:", file=sys.stderr)
    print(json.dumps(review, indent=2, sort_keys=True), file=sys.stderr)
    print(f"Type {label} to continue: ", end="", file=sys.stderr, flush=True)
    try:
        confirmation = input()
    except EOFError as exc:
        raise MemoryIntegrityError(
            f"interactive confirmation is required; type {label} in a human terminal"
        ) from exc
    if confirmation.strip() != label:
        raise MemoryIntegrityError(f"{label.lower()} confirmation was not granted")


def _timeout_seconds(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be an integer") from exc
    if not 1 <= timeout <= 1800:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 1800 seconds")
    return timeout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comeback")
    parser.add_argument("--db")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    initialize = sub.add_parser("init")
    initialize.add_argument(
        "--agent",
        choices=("codex", "claude", "both"),
        default="codex",
        help="Agent whose trust and activation steps should be shown after installation",
    )
    doctor = sub.add_parser("doctor")
    doctor.add_argument(
        "--agent",
        choices=("codex", "claude", "both"),
        default="codex",
    )
    owner = sub.add_parser("create-owner")
    owner.add_argument("--keystore")
    sub.add_parser("status")

    prepare = sub.add_parser("prepare-intervention")
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument(
        "--authorized-closer",
        help="ERC-191 signer address; defaults to the local Comeback owner keystore",
    )
    prepare.add_argument("--keystore")
    prepare.add_argument("--summary", required=True)
    prepare.add_argument(
        "--agent-scope",
        choices=("same_agent", "all_supported"),
        default="all_supported",
        help="Apply the intervention only to its source agent or to every supported coding agent",
    )
    prepare.add_argument(
        "--severity", choices=("release_blocker",), default="release_blocker"
    )
    checkpoint_source = prepare.add_mutually_exclusive_group(required=True)
    checkpoint_source.add_argument(
        "--checkpoint-command",
        help="POSIX command text; use --checkpoint-argv-json on Windows",
    )
    checkpoint_source.add_argument(
        "--checkpoint-argv-json",
        help="Exact checkpoint argument array as JSON; recommended for Windows paths",
    )
    release_source = prepare.add_mutually_exclusive_group(required=True)
    release_source.add_argument(
        "--release-command",
        help="POSIX command text; use --release-argv-json on Windows",
    )
    release_source.add_argument(
        "--release-argv-json",
        help="Exact release argument array as JSON; recommended for Windows paths",
    )
    prepare.add_argument("--checkpoint-timeout", type=_timeout_seconds, default=600)
    prepare.add_argument("--release-timeout", type=_timeout_seconds, default=600)

    intervene = sub.add_parser("intervene")
    source = intervene.add_mutually_exclusive_group(required=True)
    source.add_argument("--record", help="Signed intervention JSON")
    source.add_argument("--record-file", help="Prepared intervention JSON file")
    intervene.add_argument("--signature", help="Signature to insert into a prepared record")
    intervene.add_argument("--keystore")

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
    approve.add_argument("--approved-at")
    approve.add_argument("--signature")
    approve.add_argument("--keystore")

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--session-id", required=True)
    checkpoint.add_argument("--timeout", type=_timeout_seconds, default=600)

    release = sub.add_parser("release")
    release.add_argument("--session-id", required=True)
    release.add_argument("--timeout", type=_timeout_seconds, default=600)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--session-id", required=True)
    reconcile.add_argument(
        "--resolution", choices=("released", "not_released"), required=True
    )
    reconcile.add_argument("--resolved-at")
    reconcile.add_argument("--signature")
    reconcile.add_argument("--keystore")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "init":
            agents = ("codex", "claude") if args.agent == "both" else (args.agent,)
            result = install_repository(args.repo, agents=agents)
            machine_local_configs = " and ".join(
                "`.codex/hooks.json`"
                if agent == "codex"
                else "`.claude/settings.json`"
                for agent in agents
            )
            trust_steps: list[str] = [
                "Review and commit `.comeback-repository.json`, `.agents/skills/release-safety/SKILL.md`, and `.gitignore`.",
                f"Keep {machine_local_configs} machine-local; rerun `comeback init` in every clone.",
            ]
            if "codex" in agents:
                trust_steps.append(
                    "Open `codex` in this repository, trust the directory, run `/hooks`, "
                    "and review and trust the exact Comeback hooks."
                )
            if "claude" in agents:
                trust_steps.append(
                    "Open Claude Code in this repository and approve the reviewed Comeback hooks."
                )
            trust_steps.append(
                f"Exit the agent, then run `comeback doctor --agent {args.agent}`."
            )
            result["activation"] = {
                "gate": "PENDING",
                "verified": False,
                "agents": list(agents),
                "reason": (
                    "Installation writes project hooks, but agents must trust them before they run. "
                    "Initialization does not prove activation."
                ),
                "next": trust_steps,
            }
            result["next"] = " ".join(trust_steps)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.command == "doctor":
            agents = ("codex", "claude") if args.agent == "both" else (args.agent,)
            result = diagnose_repository(args.repo, agents=agents)
            print(json.dumps(result, indent=2, sort_keys=True))
            if result["gate"] != "PASS":
                raise SystemExit(2)
            return
        if args.command == "create-owner":
            root, _ = repository_identity(args.repo)
            result = create_owner_interactive(_keystore_path(args, root))
            result["password_protects"] = (
                "Only this local encrypted signing key; it is not a Sibyl, Codex, wallet-funds, "
                "or Base transaction password."
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        memory, repo_id = _memory(args)
        root, _ = repository_identity(args.repo)
        exit_code = 0
        if args.command == "status":
            runs = memory.list_runs()
            result = {
                "repo_id": repo_id,
                "runs": runs,
                "lessons": memory.all_lessons(),
                "inspected_at": utc_now(),
                "health": "AGENT_HOOK_OBSERVED" if runs else "NO_AGENT_HOOK_RUNS",
                "next": (
                    "Use the exact session_id when recording an intervention."
                    if runs
                    else "No lifecycle hook has reached Sibyl. Do not release and do not invoke "
                    "comeback-hook manually. Run `comeback doctor`, then start a genuinely fresh "
                    "agent session in this repository."
                ),
            }
        elif args.command == "prepare-intervention":
            closer = (
                args.authorized_closer.lower()
                if args.authorized_closer
                else owner_address(_keystore_path(args, root))
            )
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
                "action_schema": 2,
                "checkpoint_spec": {
                    "argv": _prepared_argv(
                        args.checkpoint_command,
                        args.checkpoint_argv_json,
                        "checkpoint command",
                    ),
                    "timeout_seconds": args.checkpoint_timeout,
                },
                "release_spec": {
                    "argv": _prepared_argv(
                        args.release_command,
                        args.release_argv_json,
                        "release command",
                    ),
                    "timeout_seconds": args.release_timeout,
                },
                "state_policy": {"bind_head": True, "require_clean_git": True},
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
            record = _intervention_record(args)
            if record.get("intervention_signature") in (None, "", "<SIGNATURE>"):
                signed_fields = record.get("signed_fields")
                if not isinstance(signed_fields, dict):
                    raise MemoryIntegrityError("prepared intervention has no signed_fields")
                keystore = _keystore_path(args, root)
                if owner_address(keystore) != str(
                    signed_fields.get("authorized_closer", "")
                ).lower():
                    raise MemoryIntegrityError(
                        "local owner does not match the prepared authorized closer"
                    )
                _confirm_signature(
                    "SIGN",
                    {
                        "repo_id": signed_fields.get("repo_id"),
                        "source_session_id": signed_fields.get("source_session_id"),
                        "task_class": signed_fields.get("task_class"),
                        "area": signed_fields.get("area"),
                        "agent_scope": signed_fields.get("agent_scope", "same_agent"),
                        "checkpoint_spec": signed_fields.get("checkpoint_spec"),
                        "release_spec": signed_fields.get("release_spec"),
                        "state_policy": signed_fields.get("state_policy"),
                        "authorized_closer": signed_fields.get("authorized_closer"),
                    },
                )
                record["intervention_signature"] = sign_with_owner(
                    keystore, intervention_message(signed_fields)
                )
            result = memory.record_intervention(record)
        elif args.command == "prepare-approval":
            approval_session_id = args.session_id
            run = memory.get_verified_run(approval_session_id)
            approved_at = args.approved_at or utc_now()
            result = {
                "approved_at": approved_at,
                "message_to_sign": approval_message(run, approved_at),
                "session_id": approval_session_id,
                "remaining": memory.missing_requirements(run),
            }
        elif args.command == "approve":
            approved_at = args.approved_at or utc_now()
            run = memory.get_verified_run(args.session_id)
            if args.signature:
                signature = args.signature
            else:
                _confirm_signature(
                    "APPROVE",
                    {
                        "repo_id": run["repo_id"],
                        "session_id": run["session_id"],
                        "mode": run["mode"],
                        "lesson_ids": run["lesson_ids"],
                        "checkpoint_receipt_digest": (
                            run.get("checkpoint_receipt") or {}
                        ).get("digest"),
                        "checkpoint_artifact": run.get("checkpoint_receipt"),
                        "release_spec": run.get("release_spec"),
                        "state_policy": run.get("state_policy"),
                        "remaining_before_approval": memory.missing_requirements(run),
                        "approved_at": approved_at,
                    },
                )
                signature = sign_with_owner(
                    _keystore_path(args, root), approval_message(run, approved_at)
                )
            result = memory.approve(
                args.session_id, approved_at=approved_at, signature=signature
            )
        elif args.command == "checkpoint":
            result, exit_code = execute_checkpoint(
                memory,
                session_id=args.session_id,
                root=root,
                timeout=args.timeout,
            )
        elif args.command == "release":
            result, exit_code = execute_release(
                memory,
                session_id=args.session_id,
                root=root,
                timeout=args.timeout,
            )
        elif args.command == "reconcile":
            resolved_at = args.resolved_at or utc_now()
            run = memory.get_run(args.session_id)
            lock = read_release_lock(root, repo_id)
            if args.signature:
                signature = args.signature
            else:
                _confirm_signature(
                    "RECONCILE",
                    {
                        "repo_id": run["repo_id"],
                        "session_id": run["session_id"],
                        "prior_status": run["status"],
                        "prior_outcome": run.get("outcome"),
                        "outcome_reason": run.get("outcome_reason"),
                        "release_lock": lock,
                        "resolution": args.resolution,
                        "resolved_at": resolved_at,
                        "warning": (
                            "Verify the external release target before resolving; "
                            "the local process exit code is not proof of deployment state."
                        ),
                    },
                )
                signature = sign_with_owner(
                    _keystore_path(args, root),
                    reconciliation_message(run, args.resolution, resolved_at),
                )
            result = reconcile_release(
                memory,
                session_id=args.session_id,
                root=root,
                resolution=args.resolution,
                resolved_at=resolved_at,
                signature=signature,
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
        if exit_code:
            raise SystemExit(exit_code)
    except (
        MemoryIntegrityError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(json.dumps({"decision": "refuse", "reason": str(exc)}, sort_keys=True))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
