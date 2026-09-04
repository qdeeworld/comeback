from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import NotFoundError

from .base_trust import BaseTrustError, client_for_repository
from .identity import BaseTrustConfig, tenant_id
from .policy import MODES, mode_for_outcomes, requirements_for_mode
from .signing import (
    action_spec_digest,
    approval_message,
    checkpoint_receipt_digest,
    intervention_message,
    reconciliation_fields,
    reconciliation_message,
    reconciliation_message_from_fields,
    recover_address,
)


class MemoryIntegrityError(RuntimeError):
    pass


_MUTATION_LOCK_TIMEOUT_SECONDS = 3.0


def _serialized_mutation(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        # Base is immutable verification input, not Sibyl mutation state. Keep
        # network I/O outside the cross-process SQLite mutation lock so an RPC
        # stall cannot prevent an unrelated low-risk session from opening.
        self._preverify_base_mutation(method.__name__, args, kwargs)
        with self._mutation_lock():
            return method(self, *args, **kwargs)

    return wrapped


_SUPPORTED_AGENTS = ("Codex", "ClaudeCode")
_RUN_STATUSES = {"open", "executing", "unknown", "completed", "failed"}
_CLOCK_SKEW = timedelta(seconds=60)
_MAX_CHECKPOINT_AGE = timedelta(minutes=15)
_REQUIRED_SIGNED_FIELDS = {
    "lesson_id",
    "repo_id",
    "task_class",
    "area",
    "agent_family",
    "severity",
    "action_schema",
    "checkpoint_spec",
    "release_spec",
    "state_policy",
    "required_evidence",
    "authorized_closer",
    "source_session_id",
    "incident_at",
}
_OPTIONAL_SIGNED_FIELDS = {"agent_scope"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lesson_mode(*, probation_success_count: int, unresolved_release_count: int) -> str:
    if unresolved_release_count:
        return "HUMAN_REQUIRED"
    # Every fresh intervention and every confirmed failure restarts probation.
    # One compliant release earns CHECKPOINTED; three earn AUTONOMOUS.
    return mode_for_outcomes(1, probation_success_count)


def _intervention_id(fields: dict[str, Any]) -> str:
    # This ID becomes the immutable Base commitment for the first incident.
    # Commit the exact complete signed payload, not merely its source-session
    # coordinates (and not a normalized unsigned derivative), so another
    # validly signed checkpoint/release variant cannot masquerade as the
    # anchored intervention. Canonical JSON makes dictionary ordering irrelevant.
    return hashlib.sha256(intervention_message(fields).encode("utf-8")).hexdigest()


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MemoryIntegrityError(f"{field} must be a string list")
    if len(set(value)) != len(value):
        raise MemoryIntegrityError(f"{field} cannot contain duplicates")
    return list(value)


def _argv(value: Any, field: str) -> list[str]:
    values = _strings(value, field)
    if not values or any(not item for item in values):
        raise MemoryIntegrityError(f"{field} must contain at least one non-empty argument")
    return values


def _action_spec(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryIntegrityError(f"{field} must be an object")
    if set(value) != {"argv", "timeout_seconds"}:
        raise MemoryIntegrityError(f"{field} has unsupported fields")
    argv = _argv(value.get("argv"), f"{field}.argv")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1800:
        raise MemoryIntegrityError(f"{field}.timeout_seconds must be between 1 and 1800")
    executable = Path(argv[0]).name.lower()
    secret_options = {
        "--api-key",
        "--apikey",
        "--password",
        "--private-key",
        "--secret",
        "--token",
    }
    for index, argument in enumerate(argv):
        lowered = argument.lower()
        option = lowered.split("=", 1)[0]
        if option in secret_options:
            raise MemoryIntegrityError(
                f"{field} cannot persist credential-bearing command arguments; use a credential broker or environment"
            )
    if executable in {
        "bash", "bash.exe", "cmd", "cmd.exe", "dash", "fish", "powershell",
        "powershell.exe", "pwsh", "pwsh.exe", "sh", "sh.exe", "zsh",
    }:
        raise MemoryIntegrityError(f"{field} cannot invoke a shell interpreter")
    if executable.endswith((".bat", ".cmd")):
        raise MemoryIntegrityError(
            f"{field} cannot invoke a Windows batch file; use a native executable "
            "or an explicit Python/Node entry point"
        )
    if field == "release_spec" and executable in {"git", "git.exe"}:
        if len(argv) != 4 or argv[1] != "push":
            raise MemoryIntegrityError(
                "signed Git release must be exactly `git push <direct-target> HEAD:<full-destination-ref>`"
            )
        target = argv[2]
        parsed = urlsplit(target)
        direct_target = (
            bool(parsed.scheme and parsed.netloc)
            or (target.startswith("git@") and ":" in target)
            or Path(target).is_absolute()
            or target.startswith("\\\\")
        )
        if not direct_target:
            raise MemoryIntegrityError(
                "signed Git release target must be a direct URL or absolute path, not a mutable remote name"
            )
        if parsed.scheme in {"http", "https"} and (
            parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise MemoryIntegrityError(
                "signed Git release URL cannot contain credentials, a query, or a fragment"
            )
        source, separator, destination = argv[3].partition(":")
        if (
            separator != ":"
            or source != "HEAD"
            or not destination.startswith("refs/")
            or any(character in destination for character in " \t\r\n~^:?*[\\")
        ):
            raise MemoryIntegrityError(
                "signed Git release requires `HEAD:<full-destination-ref>`; Comeback pins HEAD to the approved commit"
            )
    return {"argv": argv, "timeout_seconds": timeout}


def release_destination(spec: dict[str, Any]) -> dict[str, Any]:
    argv = spec["argv"]
    executable = Path(argv[0]).name.lower()
    if executable in {"git", "git.exe"} and len(argv) >= 4 and argv[1] == "push":
        return {
            "kind": "git_push",
            "target": argv[2],
            "refspecs": [item for item in argv[3:] if not item.startswith("-")],
        }
    return {
        "kind": "executable",
        "executable": argv[0],
        "action_spec_sha256": action_spec_digest(spec),
    }


def _state_policy(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != {"bind_head", "require_clean_git"}:
        raise MemoryIntegrityError("state_policy is invalid")
    if not all(isinstance(value[key], bool) for key in value):
        raise MemoryIntegrityError("state_policy values must be booleans")
    return {"bind_head": value["bind_head"], "require_clean_git": value["require_clean_git"]}


def _signed_intervention_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryIntegrityError("signed intervention fields must be an object")
    fields = deepcopy(value)
    keys = set(fields)
    if not _REQUIRED_SIGNED_FIELDS.issubset(keys) or not keys.issubset(
        _REQUIRED_SIGNED_FIELDS | _OPTIONAL_SIGNED_FIELDS
    ):
        raise MemoryIntegrityError("signed intervention fields have an unsupported schema")
    for name in (
        "lesson_id",
        "repo_id",
        "task_class",
        "area",
        "agent_family",
        "severity",
        "authorized_closer",
        "source_session_id",
        "incident_at",
    ):
        if not isinstance(fields.get(name), str) or not fields[name]:
            raise MemoryIntegrityError(f"signed intervention {name} is invalid")
    if fields["task_class"] != "release" or fields["area"] != "release_workflow":
        raise MemoryIntegrityError("only the release/release_workflow intervention is supported")
    if fields["severity"] != "release_blocker":
        raise MemoryIntegrityError("only release_blocker interventions are supported")
    if fields["agent_family"] not in _SUPPORTED_AGENTS:
        raise MemoryIntegrityError("intervention agent family is unsupported")
    fields["agent_scope"] = fields.get("agent_scope", "same_agent")
    if fields["agent_scope"] not in ("same_agent", "all_supported"):
        raise MemoryIntegrityError("intervention agent scope is invalid")
    expected_lesson_id = (
        f"release-release_workflow-{fields['agent_family'].lower()}"
    )
    if fields["lesson_id"] != expected_lesson_id:
        raise MemoryIntegrityError("intervention lesson ID is not the deterministic supported ID")
    if fields.get("action_schema") != 2:
        raise MemoryIntegrityError("intervention action schema is unsupported")
    fields["checkpoint_spec"] = _action_spec(
        fields.get("checkpoint_spec"), "checkpoint_spec"
    )
    fields["release_spec"] = _action_spec(fields.get("release_spec"), "release_spec")
    fields["state_policy"] = _state_policy(fields.get("state_policy"))
    required_evidence = _strings(fields.get("required_evidence"), "required_evidence")
    if sorted(required_evidence) != ["human_approval", "release_check_passed"]:
        raise MemoryIntegrityError("intervention required evidence is unsupported")
    fields["required_evidence"] = required_evidence
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", fields["authorized_closer"]):
        raise MemoryIntegrityError("intervention authorized closer is invalid")
    try:
        incident = datetime.fromisoformat(fields["incident_at"])
    except ValueError as exc:
        raise MemoryIntegrityError("intervention timestamp is invalid") from exc
    if incident.tzinfo is None:
        raise MemoryIntegrityError("intervention timestamp must include a timezone")
    return fields


def _validated_intervention_incident(
    body: Any,
    *,
    expected_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate an incident and return its stored body and normalized fields.

    The incident ID and signature are both derived from the exact stored
    ``signed_fields`` object. This deliberately preserves valid v1 records that
    omitted the optional ``agent_scope`` field while refusing IDs produced by
    the older coordinates-only algorithm.
    """

    if not isinstance(body, dict):
        raise MemoryIntegrityError(
            f"Sibyl intervention incident is invalid: {expected_id}"
        )
    signed_fields = body.get("signed_fields")
    signature = body.get("intervention_signature")
    normalized = _signed_intervention_fields(signed_fields)
    if not isinstance(signed_fields, dict) or not isinstance(signature, str):
        raise MemoryIntegrityError(
            f"Sibyl intervention incident is invalid: {expected_id}"
        )
    closer = normalized["authorized_closer"].lower()
    if (
        body.get("incident_id") != expected_id
        or _intervention_id(signed_fields) != expected_id
        or body.get("lesson_id") != normalized["lesson_id"]
        or body.get("repo_id") != normalized["repo_id"]
        or body.get("source_session_id") != normalized["source_session_id"]
        or body.get("status") != "recorded"
        or recover_address(intervention_message(signed_fields), signature) != closer
    ):
        raise MemoryIntegrityError(
            f"Sibyl intervention incident is invalid: {expected_id}"
        )
    return deepcopy(body), normalized


def _checkpoint_receipt(value: Any, run: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryIntegrityError("run checkpoint receipt is invalid")
    expected_fields = {
        "repo_id",
        "session_id",
        "checkpoint_spec_sha256",
        "release_spec_sha256",
        "state_fingerprint",
        "repository_head",
        "repository_branch",
        "release_destination",
        "started_at",
        "completed_at",
        "exit_code",
        "digest",
    }
    if set(value) != expected_fields:
        raise MemoryIntegrityError("checkpoint receipt has unsupported or missing fields")
    receipt = deepcopy(value)
    if (
        receipt.get("repo_id") != run.get("repo_id")
        or receipt.get("session_id") != run.get("session_id")
    ):
        raise MemoryIntegrityError("checkpoint receipt belongs to another run or repository")
    if receipt.get("exit_code") != 0:
        raise MemoryIntegrityError("failed checkpoint cannot create release evidence")
    checkpoint_spec = run.get("checkpoint_spec")
    if not isinstance(checkpoint_spec, dict) or receipt.get(
        "checkpoint_spec_sha256"
    ) != action_spec_digest(checkpoint_spec):
        raise MemoryIntegrityError("checkpoint receipt does not match the signed command")
    release_spec = run.get("release_spec")
    if not isinstance(release_spec, dict) or receipt.get(
        "release_spec_sha256"
    ) != action_spec_digest(release_spec):
        raise MemoryIntegrityError("checkpoint receipt does not match the signed release")
    if receipt.get("release_destination") != release_destination(release_spec):
        raise MemoryIntegrityError("checkpoint receipt release destination is invalid")
    if not isinstance(receipt.get("repository_head"), str) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", receipt["repository_head"]
    ):
        raise MemoryIntegrityError("checkpoint receipt repository head is invalid")
    if (
        not isinstance(receipt.get("repository_branch"), str)
        or not receipt["repository_branch"]
        or any(character in receipt["repository_branch"] for character in "\r\n")
    ):
        raise MemoryIntegrityError("checkpoint receipt repository branch is invalid")
    if not isinstance(receipt.get("state_fingerprint"), str) or not receipt[
        "state_fingerprint"
    ]:
        raise MemoryIntegrityError("checkpoint receipt has no repository fingerprint")
    try:
        started = datetime.fromisoformat(receipt["started_at"])
        completed = datetime.fromisoformat(receipt["completed_at"])
    except (TypeError, ValueError) as exc:
        raise MemoryIntegrityError("checkpoint receipt timestamps are invalid") from exc
    if started.tzinfo is None or completed.tzinfo is None or completed < started:
        raise MemoryIntegrityError("checkpoint receipt timestamps are invalid")
    try:
        run_started = datetime.fromisoformat(str(run["started_at"]))
    except (KeyError, ValueError) as exc:
        raise MemoryIntegrityError("run start timestamp is invalid") from exc
    now = datetime.now(timezone.utc)
    timeout = run.get("checkpoint_spec", {}).get("timeout_seconds")
    if (
        run_started.tzinfo is None
        or started < run_started - _CLOCK_SKEW
        or started > now + _CLOCK_SKEW
        or completed > now + _CLOCK_SKEW
        or not isinstance(timeout, int)
        or completed - started > timedelta(seconds=timeout) + _CLOCK_SKEW
    ):
        raise MemoryIntegrityError("checkpoint receipt chronology is invalid")
    if receipt.get("digest") != checkpoint_receipt_digest(receipt):
        raise MemoryIntegrityError("checkpoint receipt digest is invalid")
    return receipt


def validate_lesson(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise MemoryIntegrityError("lesson body is corrupted")
    required_strings = (
        "lesson_id", "repo_id", "task_class", "area", "agent_family",
        "severity", "current_mode", "authorized_closer", "source_session_id",
        "incident_at", "updated_at", "status",
    )
    for field in required_strings:
        if not isinstance(body.get(field), str) or not body[field]:
            raise MemoryIntegrityError(f"lesson {field} is invalid")
    if body["current_mode"] not in MODES:
        raise MemoryIntegrityError("lesson mode is invalid")
    if body.get("agent_scope", "same_agent") not in ("same_agent", "all_supported"):
        raise MemoryIntegrityError("lesson agent_scope is invalid")
    if body["status"] != "active":
        raise MemoryIntegrityError("lesson is not active")
    for field in (
        "failure_count",
        "success_count",
        "intervention_count",
        "probation_success_count",
        "revision",
    ):
        if not isinstance(body.get(field), int) or body[field] < 0:
            raise MemoryIntegrityError(f"lesson {field} is invalid")
    if body["intervention_count"] < 1:
        raise MemoryIntegrityError("lesson intervention count is invalid")
    if body["probation_success_count"] > body["success_count"]:
        raise MemoryIntegrityError("lesson probation successes exceed lifetime successes")
    intervention_ids = body.get("applied_intervention_ids")
    if (
        not isinstance(intervention_ids, list)
        or len(intervention_ids) != body["intervention_count"]
        or len(set(intervention_ids)) != len(intervention_ids)
        or not all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in intervention_ids)
    ):
        raise MemoryIntegrityError("lesson applied intervention IDs are invalid")
    unresolved = body.get("unresolved_release_count", 0)
    if not isinstance(unresolved, int) or unresolved < 0:
        raise MemoryIntegrityError("lesson unresolved release count is invalid")
    expected_mode = _lesson_mode(
        probation_success_count=body["probation_success_count"],
        unresolved_release_count=unresolved,
    )
    if body["current_mode"] != expected_mode:
        raise MemoryIntegrityError("lesson mode differs from its outcome history")
    body = deepcopy(body)
    body["unresolved_release_count"] = unresolved
    applied_outcomes = body.get("applied_release_outcomes", {})
    if not isinstance(applied_outcomes, dict) or not all(
        isinstance(run_id, str)
        and run_id
        and outcome in {"success", "failure", "unknown"}
        for run_id, outcome in applied_outcomes.items()
    ):
        raise MemoryIntegrityError("lesson applied release outcomes are invalid")
    body["applied_release_outcomes"] = deepcopy(applied_outcomes)
    if body.get("action_schema") != 2:
        raise MemoryIntegrityError("lesson action schema is unsupported; create a new signed v2 intervention")
    body["agent_scope"] = body.get("agent_scope", "same_agent")
    body["required_evidence"] = _strings(body.get("required_evidence"), "required_evidence")
    body["checkpoint_spec"] = _action_spec(body.get("checkpoint_spec"), "checkpoint_spec")
    body["release_spec"] = _action_spec(body.get("release_spec"), "release_spec")
    body["state_policy"] = _state_policy(body.get("state_policy"))
    signature = body.get("intervention_signature")
    signed_fields = body.get("signed_fields")
    if not isinstance(signature, str) or not isinstance(signed_fields, dict):
        raise MemoryIntegrityError("lesson lacks signed intervention provenance")
    normalized_signed_fields = _signed_intervention_fields(signed_fields)
    signed_scope = normalized_signed_fields["agent_scope"]
    if body["agent_scope"] != signed_scope:
        raise MemoryIntegrityError("lesson agent_scope differs from its signed scope")
    signer = recover_address(intervention_message(signed_fields), signature)
    if signer != body["authorized_closer"].lower():
        raise MemoryIntegrityError("lesson intervention signature is invalid")
    for key, expected in normalized_signed_fields.items():
        if body.get(key) != expected:
            raise MemoryIntegrityError(f"signed lesson field changed: {key}")
    return body


def validate_run(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise MemoryIntegrityError("run body is corrupted")
    for field in ("session_id", "repo_id", "task_class", "area", "agent_family", "mode", "status"):
        if not isinstance(body.get(field), str) or not body[field]:
            raise MemoryIntegrityError(f"run {field} is invalid")
    if body["mode"] not in MODES:
        raise MemoryIntegrityError("run mode is invalid")
    if body["status"] not in _RUN_STATUSES:
        raise MemoryIntegrityError("run status is invalid")
    run = deepcopy(body)
    run["lesson_ids"] = _strings(run.get("lesson_ids"), "lesson_ids")
    lesson_revisions = run.get("lesson_revisions", {})
    if (
        not isinstance(lesson_revisions, dict)
        or set(lesson_revisions) != set(run["lesson_ids"])
        or not all(
            isinstance(revision, int) and revision >= 1
            for revision in lesson_revisions.values()
        )
    ):
        raise MemoryIntegrityError("run lesson revision binding is invalid")
    run["lesson_revisions"] = deepcopy(lesson_revisions)
    run["required_evidence"] = _strings(run.get("required_evidence"), "required_evidence")
    run["satisfied_evidence"] = _strings(run.get("satisfied_evidence"), "satisfied_evidence")
    if not set(run["satisfied_evidence"]).issubset(run["required_evidence"]):
        raise MemoryIntegrityError("run contains unsupported satisfied evidence")
    if run["lesson_ids"]:
        if run.get("action_schema") != 2:
            raise MemoryIntegrityError("run action schema is unsupported")
        run["checkpoint_spec"] = _action_spec(run.get("checkpoint_spec"), "checkpoint_spec")
        run["release_spec"] = _action_spec(run.get("release_spec"), "release_spec")
        run["state_policy"] = _state_policy(run.get("state_policy"))
    else:
        run["action_schema"] = None
        run["checkpoint_spec"] = None
        run["release_spec"] = None
        run["state_policy"] = None
    receipt_value = run.get("checkpoint_receipt")
    receipt = (
        _checkpoint_receipt(receipt_value, run)
        if receipt_value is not None
        else None
    )
    run["checkpoint_receipt"] = receipt
    has_checkpoint = "release_check_passed" in run["satisfied_evidence"]
    if has_checkpoint != isinstance(receipt, dict):
        raise MemoryIntegrityError("run checkpoint evidence and receipt disagree")
    checkpoint_attempt = run.get("checkpoint_attempt")
    if checkpoint_attempt is not None:
        if (
            not isinstance(checkpoint_attempt, dict)
            or set(checkpoint_attempt) != {"attempt_id", "started_at"}
            or not isinstance(checkpoint_attempt.get("attempt_id"), str)
            or not checkpoint_attempt["attempt_id"]
        ):
            raise MemoryIntegrityError("run checkpoint attempt is invalid")
        try:
            attempt_started = datetime.fromisoformat(checkpoint_attempt["started_at"])
        except (TypeError, ValueError) as exc:
            raise MemoryIntegrityError("run checkpoint attempt timestamp is invalid") from exc
        if attempt_started.tzinfo is None:
            raise MemoryIntegrityError("run checkpoint attempt timestamp is invalid")
        if run["status"] != "open":
            raise MemoryIntegrityError("closed run contains an active checkpoint attempt")
        if receipt is not None or has_checkpoint:
            raise MemoryIntegrityError(
                "active checkpoint attempt cannot retain earlier checkpoint evidence"
            )
    run["checkpoint_attempt"] = deepcopy(checkpoint_attempt)
    approval = run.get("approval")
    has_approval = "human_approval" in run["satisfied_evidence"]
    if has_approval:
        if not isinstance(approval, dict) or set(approval) != {
            "approved_at",
            "signer",
            "signature",
        }:
            raise MemoryIntegrityError("run human approval is invalid")
        if not all(isinstance(approval.get(key), str) and approval[key] for key in approval):
            raise MemoryIntegrityError("run human approval is invalid")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", approval["signer"]):
            raise MemoryIntegrityError("run human approval signer is invalid")
        try:
            approved_at = datetime.fromisoformat(approval["approved_at"])
        except ValueError as exc:
            raise MemoryIntegrityError("run human approval timestamp is invalid") from exc
        if approved_at.tzinfo is None:
            raise MemoryIntegrityError("run human approval timestamp is invalid")
        if receipt is not None:
            receipt_completed = datetime.fromisoformat(receipt["completed_at"])
            if approved_at < receipt_completed:
                raise MemoryIntegrityError("human approval predates its checkpoint")
    elif approval is not None:
        raise MemoryIntegrityError("run contains approval without satisfied evidence")
    for field in ("started_at", "updated_at"):
        try:
            timestamp = datetime.fromisoformat(str(run.get(field, "")))
        except ValueError as exc:
            raise MemoryIntegrityError(f"run {field} is invalid") from exc
        if timestamp.tzinfo is None:
            raise MemoryIntegrityError(f"run {field} is invalid")
    process_id = run.get("process_id")
    if process_id is not None and (
        not isinstance(process_id, int) or isinstance(process_id, bool) or process_id < 1
    ):
        raise MemoryIntegrityError("run process ID is invalid")
    outcome = run.get("outcome")
    if run["status"] == "executing":
        try:
            release_started = datetime.fromisoformat(str(run.get("release_started_at", "")))
        except ValueError as exc:
            raise MemoryIntegrityError("executing run release timestamp is invalid") from exc
        if release_started.tzinfo is None:
            raise MemoryIntegrityError("executing run release timestamp is invalid")
        if outcome is not None:
            raise MemoryIntegrityError("executing run cannot contain a release outcome")
    elif run["status"] == "unknown":
        if outcome != "unknown" or not isinstance(run.get("outcome_reason"), str):
            raise MemoryIntegrityError("unknown run outcome is inconsistent")
    elif run["status"] == "completed" and outcome != "success":
        raise MemoryIntegrityError("completed run outcome is inconsistent")
    elif run["status"] == "failed" and outcome != "failure":
        raise MemoryIntegrityError("failed run outcome is inconsistent")
    elif run["status"] == "open" and outcome is not None:
        raise MemoryIntegrityError("open run cannot contain a release outcome")

    reconciliation = run.get("reconciliation")
    if reconciliation is not None:
        if not isinstance(reconciliation, dict) or set(reconciliation) != {
            "resolution",
            "resolved_at",
            "signer",
            "signature",
            "signed_fields",
        }:
            raise MemoryIntegrityError("run reconciliation record is invalid")
        if not all(
            isinstance(reconciliation.get(field), str) and reconciliation[field]
            for field in ("resolution", "resolved_at", "signer", "signature")
        ) or not isinstance(reconciliation.get("signed_fields"), dict):
            raise MemoryIntegrityError("run reconciliation record is invalid")
        signed_fields = reconciliation["signed_fields"]
        if set(signed_fields) != {
            "repo_id",
            "session_id",
            "lesson_ids",
            "prior_status",
            "prior_outcome",
            "outcome_reason",
            "resolution",
            "resolved_at",
        }:
            raise MemoryIntegrityError("run reconciliation signed fields are invalid")
        resolution = reconciliation["resolution"]
        if resolution not in {"released", "not_released"}:
            raise MemoryIntegrityError("run reconciliation resolution is invalid")
        if (run["status"], run["outcome"]) != (
            ("completed", "success")
            if resolution == "released"
            else ("failed", "failure")
        ):
            raise MemoryIntegrityError("run reconciliation conflicts with its outcome")
        if (
            signed_fields.get("repo_id") != run["repo_id"]
            or signed_fields.get("session_id") != run["session_id"]
            or signed_fields.get("lesson_ids") != run["lesson_ids"]
            or signed_fields.get("resolution") != resolution
            or signed_fields.get("resolved_at") != reconciliation["resolved_at"]
        ):
            raise MemoryIntegrityError("run reconciliation binding is invalid")
        prior_status = signed_fields.get("prior_status")
        expected_prior_outcomes = {
            "open": "",
            "executing": "",
            "unknown": "unknown",
            "completed": "success",
            "failed": "failure",
        }
        if (
            prior_status not in expected_prior_outcomes
            or signed_fields.get("prior_outcome")
            != expected_prior_outcomes[prior_status]
            or not isinstance(signed_fields.get("outcome_reason"), str)
        ):
            raise MemoryIntegrityError("run reconciliation prior state is invalid")
        try:
            resolved = datetime.fromisoformat(reconciliation["resolved_at"])
        except ValueError as exc:
            raise MemoryIntegrityError("run reconciliation timestamp is invalid") from exc
        if (
            resolved.tzinfo is None
            or resolved > datetime.now(timezone.utc) + _CLOCK_SKEW
        ):
            raise MemoryIntegrityError("run reconciliation timestamp is invalid")
        chronology_field = "started_at" if prior_status == "open" else "release_started_at"
        try:
            chronology_start = datetime.fromisoformat(
                str(run.get(chronology_field, ""))
            )
        except ValueError as exc:
            raise MemoryIntegrityError("run reconciliation chronology is invalid") from exc
        if chronology_start.tzinfo is None or resolved < chronology_start:
            raise MemoryIntegrityError("run reconciliation chronology is invalid")
        signer = reconciliation["signer"].lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", signer):
            raise MemoryIntegrityError("run reconciliation signer is invalid")
        try:
            recovered = recover_address(
                reconciliation_message_from_fields(signed_fields),
                reconciliation["signature"],
            )
        except Exception as exc:
            raise MemoryIntegrityError("run reconciliation signature is invalid") from exc
        if recovered != signer:
            raise MemoryIntegrityError("run reconciliation signature is invalid")
        run["reconciliation"] = deepcopy(reconciliation)
    return run


class InterventionMemory:
    LESSON_CATEGORY = "intervention_lesson"
    INCIDENT_CATEGORY = "intervention_incident"
    RUN_CATEGORY = "supervision_run"

    def __init__(
        self,
        db_path: str | Path,
        repo_id: str,
        *,
        base_trust: BaseTrustConfig | None = None,
        base_client: Any | None = None,
    ) -> None:
        if base_client is not None and base_trust is None:
            raise ValueError("a Base client requires committed Base trust configuration")
        self.repo_id = repo_id
        self.db_path = Path(db_path).expanduser().resolve()
        self.client = MemoryClient.local(self.db_path, tenant_id=tenant_id(repo_id))
        self.base_trust = base_trust
        self.base_client = (
            base_client
            if base_client is not None
            else (client_for_repository(base_trust) if base_trust is not None else None)
        )
        self._verified_base_state: Any | None = None

    def close(self) -> None:
        """Release Sibyl's cached SQLite handles deterministically.

        Windows does not permit a temporary directory to be removed while its
        SQLite database or WAL is still open.  Diagnostic probes deliberately
        create short-lived stores, so relying on garbage collection would turn
        a successful hook check into a platform-specific cleanup failure.
        """

        self.client.storage.close()

    def __enter__(self) -> "InterventionMemory":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _load_all_lessons(self) -> list[dict[str, Any]]:
        lessons: list[dict[str, Any]] = []
        for source_agent in _SUPPORTED_AGENTS:
            lesson_id = f"release-release_workflow-{source_agent.lower()}"
            try:
                entity = self.client.get_entity(self.LESSON_CATEGORY, lesson_id)
            except NotFoundError:
                continue
            lesson = validate_lesson(entity.get("body"))
            if lesson["repo_id"] == self.repo_id:
                lessons.append(lesson)
        return sorted(lessons, key=lambda lesson: lesson["lesson_id"])

    def _load_intervention_incident(
        self,
        intervention_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            body = self.client.get_entity(
                self.INCIDENT_CATEGORY,
                intervention_id,
            ).get("body")
        except NotFoundError as exc:
            raise MemoryIntegrityError(
                f"Sibyl intervention incident is missing: {intervention_id}"
            ) from exc
        body, fields = _validated_intervention_incident(
            body,
            expected_id=intervention_id,
        )
        if fields["repo_id"] != self.repo_id:
            raise MemoryIntegrityError(
                f"Sibyl intervention incident is invalid: {intervention_id}"
            )
        return body, fields

    def anchorable_intervention_id(self, *, owner: str) -> str:
        """Return the one full-payload intervention that Base may activate."""

        owner = owner.lower()
        lessons = self.all_lessons()
        if not lessons:
            raise MemoryIntegrityError(
                "Base activation requires one signed Sibyl intervention"
            )
        if any(lesson["authorized_closer"] != owner for lesson in lessons):
            raise MemoryIntegrityError(
                "Sibyl intervention authority differs from the Base repository owner"
            )
        intervention_ids = {
            intervention_id
            for lesson in lessons
            for intervention_id in lesson["applied_intervention_ids"]
        }
        if len(intervention_ids) != 1:
            raise MemoryIntegrityError(
                "Base activation requires exactly one initial Sibyl intervention"
            )
        intervention_id = next(iter(intervention_ids))
        _, fields = self._load_intervention_incident(intervention_id)
        if fields["authorized_closer"].lower() != owner or not any(
            lesson["lesson_id"] == fields["lesson_id"]
            and intervention_id in lesson["applied_intervention_ids"]
            for lesson in lessons
        ):
            raise MemoryIntegrityError(
                f"Sibyl intervention incident is invalid: {intervention_id}"
            )
        return intervention_id

    def _verify_base_trust(
        self,
        lessons: list[dict[str, Any]],
        *,
        require_memory: bool,
    ) -> Any | None:
        trust = self.base_trust
        if trust is None:
            return None
        if self.base_client is None:
            raise MemoryIntegrityError("Base trust client is unavailable")
        try:
            derived_key = self.base_client.anchor_key(
                repo_id=self.repo_id,
                nonce=trust.nonce,
                owner=trust.owner_address,
            )
            if derived_key != trust.anchor_key:
                raise MemoryIntegrityError(
                    "committed Base anchor key does not match repository identity"
                )
            if self._verified_base_state is None:
                if trust.status == "active":
                    self._verified_base_state = self.base_client.verify_active(
                        repo_id=self.repo_id,
                        nonce=trust.nonce,
                        owner=trust.owner_address,
                        initial_intervention_id=str(trust.initial_intervention_id),
                    )
                else:
                    self._verified_base_state = self.base_client.verify_claim(
                        repo_id=self.repo_id,
                        nonce=trust.nonce,
                        owner=trust.owner_address,
                    )
        except MemoryIntegrityError:
            raise
        except (BaseTrustError, RuntimeError, ValueError) as exc:
            raise MemoryIntegrityError(f"Base trust verification failed: {exc}") from exc

        if any(
            lesson["authorized_closer"].lower() != trust.owner_address
            for lesson in lessons
        ):
            raise MemoryIntegrityError(
                "Sibyl intervention authority differs from the Base repository owner"
            )
        if trust.status == "active" and require_memory:
            initial_id = trust.initial_intervention_id
            matching_lessons = (
                [
                    lesson
                    for lesson in lessons
                    if isinstance(initial_id, str)
                    and initial_id in lesson["applied_intervention_ids"]
                ]
                if isinstance(initial_id, str)
                else []
            )
            if not matching_lessons:
                raise MemoryIntegrityError(
                    "Base expects an activated Sibyl intervention that is missing; restore the memory store"
                )
            try:
                _, fields = self._load_intervention_incident(initial_id)
            except MemoryIntegrityError as exc:
                raise MemoryIntegrityError(
                    "Base expects an activated Sibyl intervention that is missing or invalid; restore the memory store"
                ) from exc
            if (
                fields["authorized_closer"].lower() != trust.owner_address
                or not any(
                    lesson["lesson_id"] == fields["lesson_id"]
                    for lesson in matching_lessons
                )
            ):
                raise MemoryIntegrityError(
                    "Base expects an activated Sibyl intervention that is missing or invalid; restore the memory store"
                )
        return self._verified_base_state

    def _preverify_base_mutation(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        if self.base_trust is None:
            return
        if method_name == "start_run":
            if kwargs.get("task_class") != "release":
                return
        elif method_name == "record_intervention":
            pass
        elif method_name in {
            "record_pretool_decision",
            "begin_checkpoint_attempt",
            "finish_checkpoint_attempt",
            "record_checkpoint_receipt",
            "approve",
            "begin_release",
        }:
            session_id = kwargs.get("session_id")
            if session_id is None and args:
                session_id = args[0]
            run = self.get_run(str(session_id))
            if run["task_class"] != "release":
                return
        else:
            # Outcome and reconciliation writes must remain available after a
            # release side effect even if Base is temporarily unreachable.
            return
        self._verify_base_trust(
            self._load_all_lessons(),
            require_memory=self.base_trust.status == "active",
        )

    @contextmanager
    def _mutation_lock(self):
        lock_path = self.db_path.parent / f"memory-{self.repo_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + _MUTATION_LOCK_TIMEOUT_SECONDS
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise MemoryIntegrityError(
                                "timed out waiting for the Sibyl mutation lock"
                            ) from exc
                        time.sleep(0.05)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, OSError) as exc:
                        if time.monotonic() >= deadline:
                            raise MemoryIntegrityError(
                                "timed out waiting for the Sibyl mutation lock"
                            ) from exc
                        time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @_serialized_mutation
    def record_intervention(self, record: dict[str, Any]) -> dict[str, Any]:
        signed_fields = record.get("signed_fields")
        signature = record.get("intervention_signature")
        if not isinstance(signed_fields, dict) or not isinstance(signature, str):
            raise MemoryIntegrityError("signed intervention fields are required")
        normalized_signed_fields = _signed_intervention_fields(signed_fields)
        if normalized_signed_fields["repo_id"] != self.repo_id:
            raise MemoryIntegrityError("intervention belongs to another repository")
        source_run = self.get_run(normalized_signed_fields["source_session_id"])
        for field in ("repo_id", "task_class", "area", "agent_family"):
            if source_run.get(field) != normalized_signed_fields[field]:
                raise MemoryIntegrityError(
                    f"intervention {field} does not match its Sibyl source run"
                )
        closer = normalized_signed_fields["authorized_closer"].lower()
        if recover_address(intervention_message(signed_fields), signature) != closer:
            raise MemoryIntegrityError("intervention signature is invalid")
        existing_lessons = self._load_all_lessons()
        self._verify_base_trust(
            existing_lessons,
            require_memory=self.base_trust is not None
            and self.base_trust.status == "active",
        )
        if self.base_trust is not None and closer != self.base_trust.owner_address:
            raise MemoryIntegrityError(
                "intervention is not signed by the Base repository owner"
            )
        lesson_id = normalized_signed_fields["lesson_id"]
        for existing in existing_lessons:
            if existing["authorized_closer"] != closer:
                raise MemoryIntegrityError(
                    "authorized closer is already anchored for this repository"
                )
            scopes_overlap = (
                existing["lesson_id"] != lesson_id
                and (
                    existing["agent_scope"] == "all_supported"
                or normalized_signed_fields["agent_scope"] == "all_supported"
                )
            )
            if scopes_overlap and any(
                existing[field] != normalized_signed_fields[field]
                for field in ("checkpoint_spec", "release_spec", "state_policy")
            ):
                raise MemoryIntegrityError(
                    "overlapping agent scopes require identical signed action and state policies"
                )
        # The Base commitment must be a digest of the payload the owner
        # actually signed. A legacy record may omit optional agent_scope; its
        # default is used for behavior but is not silently inserted into the
        # signed preimage.
        incident_id = _intervention_id(signed_fields)
        for existing in existing_lessons:
            for recorded_id in existing["applied_intervention_ids"]:
                if recorded_id == incident_id:
                    # Preserve projection-first repair for an exact retry. The
                    # incident entity itself is compared later in this method.
                    continue
                _, historical_fields = self._load_intervention_incident(recorded_id)
                if (
                    historical_fields["lesson_id"] != existing["lesson_id"]
                    or historical_fields["authorized_closer"].lower()
                    != existing["authorized_closer"]
                ):
                    raise MemoryIntegrityError(
                        f"Sibyl intervention incident is invalid: {recorded_id}"
                    )
                if (
                    historical_fields["source_session_id"]
                    == normalized_signed_fields["source_session_id"]
                ):
                    raise MemoryIntegrityError(
                        "this Sibyl source run already has a different intervention"
                    )
        if self.base_trust is not None and self.base_trust.status == "claimed":
            recorded_ids = {
                recorded
                for existing in existing_lessons
                for recorded in existing["applied_intervention_ids"]
            }
            if recorded_ids and incident_id not in recorded_ids:
                raise MemoryIntegrityError(
                    "activate the first Base-backed intervention before recording another"
                )
        incident_summary = str(record.get("incident_summary", ""))
        incident_body = {
            "incident_id": incident_id,
            "lesson_id": lesson_id,
            "repo_id": self.repo_id,
            "source_session_id": normalized_signed_fields["source_session_id"],
            "signed_fields": deepcopy(signed_fields),
            "intervention_signature": signature,
            "incident_summary": incident_summary,
            "recorded_at": utc_now(),
            "status": "recorded",
        }
        try:
            prior_lesson = validate_lesson(
                self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
            )
        except NotFoundError:
            prior_lesson = None
        if prior_lesson is not None and closer != prior_lesson["authorized_closer"]:
            raise MemoryIntegrityError(
                "authorized closer is already anchored for this intervention lesson"
            )
        try:
            existing_incident = self.client.get_entity(
                self.INCIDENT_CATEGORY, incident_id
            ).get("body")
        except NotFoundError:
            existing_incident = None
        if existing_incident is not None:
            comparable = {
                key: existing_incident.get(key)
                for key in (
                    "incident_id",
                    "lesson_id",
                    "repo_id",
                    "source_session_id",
                    "signed_fields",
                    "intervention_signature",
                    "incident_summary",
                    "status",
                )
            }
            expected = {
                key: incident_body[key]
                for key in comparable
            }
            if comparable != expected:
                raise MemoryIntegrityError(
                    "this Sibyl source run already has a different intervention"
                )
            try:
                return validate_lesson(
                    self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
                )
            except NotFoundError as exc:
                raise MemoryIntegrityError(
                    "intervention incident exists without its lesson projection"
                ) from exc
        failures = 1
        successes = 0
        intervention_count = 1
        probation_success_count = 0
        unresolved_releases = 0
        applied_release_outcomes: dict[str, str] = {}
        applied_intervention_ids = [incident_id]
        revision = 1
        if prior_lesson is not None:
            prior = prior_lesson
            if incident_id in prior["applied_intervention_ids"]:
                # Repair a projection-first partial write without counting the
                # same source session twice.
                self.client.set_entity(
                    self.INCIDENT_CATEGORY,
                    incident_id,
                    incident_body,
                    status="recorded",
                )
                return prior
            failures = prior["failure_count"] + 1
            successes = prior["success_count"]
            intervention_count = prior["intervention_count"] + 1
            probation_success_count = 0
            unresolved_releases = prior["unresolved_release_count"]
            applied_release_outcomes = deepcopy(prior["applied_release_outcomes"])
            applied_intervention_ids = [
                *prior["applied_intervention_ids"],
                incident_id,
            ]
            revision = prior["revision"] + 1
        body = {
            **normalized_signed_fields,
            "authorized_closer": closer,
            "intervention_signature": signature,
            "signed_fields": deepcopy(signed_fields),
            "incident_summary": incident_summary,
            "failure_count": failures,
            "success_count": successes,
            "intervention_count": intervention_count,
            "probation_success_count": probation_success_count,
            "unresolved_release_count": unresolved_releases,
            "applied_release_outcomes": applied_release_outcomes,
            "applied_intervention_ids": applied_intervention_ids,
            "current_mode": _lesson_mode(
                probation_success_count=probation_success_count,
                unresolved_release_count=unresolved_releases,
            ),
            "revision": revision,
            "updated_at": utc_now(),
            "status": "active",
        }
        validated = validate_lesson(body)
        self.client.set_entity(self.LESSON_CATEGORY, lesson_id, validated, status="active")
        self.client.set_entity(
            self.INCIDENT_CATEGORY,
            incident_id,
            incident_body,
            status="recorded",
        )
        try:
            self.client.write_event(
                evaluated={
                    "repo_id": self.repo_id,
                    "task_class": validated["task_class"],
                    "source_session_id": validated["source_session_id"],
                    "incident_id": incident_id,
                },
                acted={
                    "event": "human_intervention",
                    "lesson_id": lesson_id,
                    "signed_fields": deepcopy(signed_fields),
                    "intervention_signature": signature,
                    "incident_summary": incident_summary,
                },
                forward={
                    "mode": validated["current_mode"],
                    "revision": validated["revision"],
                },
            )
        except Exception:
            # Entity projections are authoritative. A secondary journal
            # outage must not make a committed intervention look rejected.
            pass
        return validated

    def matching_lessons(self, task_class: str, area: str, agent_family: str) -> list[dict[str, Any]]:
        lessons = self._load_all_lessons()
        if task_class == "release":
            self._verify_base_trust(lessons, require_memory=True)
        matches = []
        for lesson in lessons:
            agent_scope = lesson.get("agent_scope", "same_agent")
            applies_to_agent = (
                agent_scope == "all_supported"
                or lesson["agent_family"].lower() == agent_family.lower()
            )
            if (
                lesson["repo_id"] == self.repo_id
                and lesson["task_class"] == task_class
                and lesson["area"] == area
                and applies_to_agent
            ):
                matches.append(lesson)
        return matches

    def all_lessons(self) -> list[dict[str, Any]]:
        lessons = self._load_all_lessons()
        self._verify_base_trust(
            lessons,
            require_memory=self.base_trust is not None
            and self.base_trust.status == "active",
        )
        return lessons

    def configured_release_argvs(self, agent_family: str) -> list[list[str]]:
        """Return signed release shapes for candidate classification only.

        This deliberately avoids a Base RPC call. The hook uses it only to
        decide whether an action is potentially protected; the actual run and
        policy are always verified against Base before an allow decision.
        """

        arguments: list[list[str]] = []
        for lesson in self._load_all_lessons():
            scope = lesson.get("agent_scope", "same_agent")
            if scope == "all_supported" or lesson["agent_family"].lower() == agent_family.lower():
                arguments.append(list(lesson["release_spec"]["argv"]))
        return arguments

    @_serialized_mutation
    def record_pretool_decision(
        self,
        *,
        session_id: str,
        tool_use_id: str,
        command: str,
        action_kind: str,
        decision: str,
        reason: str,
    ) -> str:
        if decision not in {"allow", "deny"}:
            raise MemoryIntegrityError("pretool decision is invalid")
        if action_kind not in {
            "checkpoint_capability",
            "release_capability",
            "noncanonical_capability",
            "raw_release",
        }:
            raise MemoryIntegrityError("pretool action kind is invalid")
        # Requiring an existing run binds this evidence to UserPromptSubmit and
        # prevents a standalone/manual hook invocation from fabricating a full
        # lifecycle proof.
        run = self.get_run(session_id)
        return self.client.write_event(
            evaluated={
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                "action_kind": action_kind,
                "mode": run["mode"],
            },
            acted={"event": "pretool_decision", "decision": decision},
            forward={"reason": reason},
        )

    def pretool_decisions(self, session_id: str) -> list[dict[str, Any]]:
        return [
            event
            for event in self.client.read_events(limit=100)
            if event.get("acted", {}).get("event") == "pretool_decision"
            and event.get("evaluated", {}).get("session_id") == session_id
        ]

    @_serialized_mutation
    def start_run(
        self,
        *,
        session_id: str,
        task_class: str,
        area: str,
        agent_family: str,
        model: str,
        process_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        try:
            existing = self.get_run(session_id)
        except MemoryIntegrityError as exc:
            if "no Sibyl supervision run exists" not in str(exc):
                raise
        else:
            if existing["status"] == "open" and not force:
                return self.get_verified_run(session_id)
            if existing["status"] != "open":
                return existing
        lessons = self.matching_lessons(task_class, area, agent_family)
        checkpoint_specs = {
            json.dumps(lesson["checkpoint_spec"], sort_keys=True): lesson["checkpoint_spec"]
            for lesson in lessons
        }
        if len(checkpoint_specs) > 1:
            raise MemoryIntegrityError("conflicting remembered checkpoint specs require operator resolution")
        release_specs = {
            json.dumps(lesson["release_spec"], sort_keys=True): lesson["release_spec"]
            for lesson in lessons
        }
        if len(release_specs) > 1:
            raise MemoryIntegrityError("conflicting remembered release specs require operator resolution")
        state_policies = {
            json.dumps(lesson["state_policy"], sort_keys=True): lesson["state_policy"]
            for lesson in lessons
        }
        if len(state_policies) > 1:
            raise MemoryIntegrityError("conflicting remembered state policies require operator resolution")
        rank = {"AUTONOMOUS": 0, "CHECKPOINTED": 1, "HUMAN_REQUIRED": 2}
        mode = max((lesson["current_mode"] for lesson in lessons), key=rank.get, default="AUTONOMOUS")
        run = {
            "session_id": session_id,
            "repo_id": self.repo_id,
            "task_class": task_class,
            "area": area,
            "agent_family": agent_family,
            "model": model,
            "process_id": process_id,
            "mode": mode,
            "lesson_ids": sorted(lesson["lesson_id"] for lesson in lessons),
            "lesson_revisions": {
                lesson["lesson_id"]: lesson["revision"] for lesson in lessons
            },
            "action_schema": 2 if lessons else None,
            "checkpoint_spec": deepcopy(next(iter(checkpoint_specs.values()), None)),
            "release_spec": deepcopy(next(iter(release_specs.values()), None)),
            "state_policy": deepcopy(next(iter(state_policies.values()), None)),
            "checkpoint_receipt": None,
            "checkpoint_attempt": None,
            "required_evidence": requirements_for_mode(mode),
            "satisfied_evidence": [],
            "status": "open",
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "task_class": task_class, "lesson_ids": run["lesson_ids"]},
                acted={"event": "supervision_selected", "mode": mode},
                forward={"required_evidence": run["required_evidence"]},
            )
        except Exception:
            pass
        return run

    def get_run(self, session_id: str) -> dict[str, Any]:
        try:
            entity = self.client.get_entity(self.RUN_CATEGORY, session_id)
        except NotFoundError as exc:
            raise MemoryIntegrityError("no Sibyl supervision run exists for this session") from exc
        run = validate_run(entity.get("body"))
        reconciliation = run.get("reconciliation")
        if reconciliation is not None:
            lessons = [
                validate_lesson(
                    self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
                )
                for lesson_id in run["lesson_ids"]
            ]
            closers = {lesson["authorized_closer"].lower() for lesson in lessons}
            if len(closers) != 1 or reconciliation["signer"].lower() not in closers:
                raise MemoryIntegrityError(
                    "run reconciliation is not signed by the authorized closer"
                )
        return run

    def get_verified_run(self, session_id: str) -> dict[str, Any]:
        run = self.get_run(session_id)
        lessons = self.matching_lessons(
            run["task_class"], run["area"], run["agent_family"]
        )
        expected_ids = sorted(lesson["lesson_id"] for lesson in lessons)
        if run["lesson_ids"] != expected_ids:
            raise MemoryIntegrityError("supervision run lesson binding is corrupted or stale")
        expected_revisions = {
            lesson["lesson_id"]: lesson["revision"] for lesson in lessons
        }
        if run["lesson_revisions"] != expected_revisions:
            raise MemoryIntegrityError("supervision run lesson revisions are stale")
        rank = {"AUTONOMOUS": 0, "CHECKPOINTED": 1, "HUMAN_REQUIRED": 2}
        expected_mode = max(
            (lesson["current_mode"] for lesson in lessons),
            key=rank.get,
            default="AUTONOMOUS",
        )
        if run["mode"] != expected_mode:
            raise MemoryIntegrityError("supervision run mode differs from signed lesson state")
        if run["required_evidence"] != requirements_for_mode(expected_mode):
            raise MemoryIntegrityError("supervision run requirements are corrupted")
        if lessons:
            for field in ("action_schema", "checkpoint_spec", "release_spec", "state_policy"):
                expected_values = {
                    json.dumps(lesson.get(field), sort_keys=True): lesson.get(field)
                    for lesson in lessons
                }
                if len(expected_values) != 1:
                    raise MemoryIntegrityError(
                        f"conflicting remembered {field} values require operator resolution"
                    )
                expected = next(iter(expected_values.values()))
                if run.get(field) != expected:
                    raise MemoryIntegrityError(f"supervision run {field} differs from signed lesson")
        if "human_approval" in run["satisfied_evidence"]:
            approval = run["approval"]
            closers = {lesson["authorized_closer"].lower() for lesson in lessons}
            if len(closers) != 1:
                raise MemoryIntegrityError("conflicting authorized closers require operator recovery")
            try:
                recovered = recover_address(
                    approval_message(run, approval["approved_at"]), approval["signature"]
                )
            except Exception as exc:
                raise MemoryIntegrityError("stored human approval signature is invalid") from exc
            if recovered not in closers or recovered != approval["signer"].lower():
                raise MemoryIntegrityError("stored human approval signature is invalid")
        return run

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        runs = [
            validate_run(entity.get("body"))
            for entity in self.client.list_entities(self.RUN_CATEGORY, limit=limit)
        ]
        return sorted(runs, key=lambda run: run.get("updated_at", ""), reverse=True)

    @_serialized_mutation
    def begin_checkpoint_attempt(
        self, session_id: str, *, attempt_id: str, started_at: str
    ) -> dict[str, Any]:
        run = self.get_verified_run(session_id)
        if run["status"] != "open":
            raise MemoryIntegrityError("supervision run is not open")
        if run.get("checkpoint_attempt") is not None:
            raise MemoryIntegrityError(
                "another checkpoint attempt is active; start a fresh supervision session "
                "if its process was interrupted"
            )
        if not isinstance(attempt_id, str) or not attempt_id:
            raise MemoryIntegrityError("checkpoint attempt ID is invalid")
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise MemoryIntegrityError("checkpoint attempt timestamp is invalid") from exc
        if started.tzinfo is None or started > datetime.now(timezone.utc) + _CLOCK_SKEW:
            raise MemoryIntegrityError("checkpoint attempt timestamp is invalid")
        run["checkpoint_receipt"] = None
        run["satisfied_evidence"] = [
            item
            for item in run["satisfied_evidence"]
            if item not in {"release_check_passed", "human_approval"}
        ]
        run.pop("approval", None)
        run["checkpoint_attempt"] = {
            "attempt_id": attempt_id,
            "started_at": started_at,
        }
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "attempt_id": attempt_id},
                acted={"event": "checkpoint_started"},
                forward={"remaining": self.missing_requirements(run)},
            )
        except Exception:
            # The entity transition is the safety boundary. A journal outage
            # must not restore stale checkpoint evidence or start the command
            # without its durable invalidation.
            pass
        return run

    @_serialized_mutation
    def finish_checkpoint_attempt(
        self, session_id: str, *, attempt_id: str, reason: str
    ) -> dict[str, Any]:
        run = self.get_verified_run(session_id)
        attempt = run.get("checkpoint_attempt")
        if not isinstance(attempt, dict) or attempt.get("attempt_id") != attempt_id:
            raise MemoryIntegrityError("checkpoint attempt identity is stale or missing")
        run["checkpoint_attempt"] = None
        run["checkpoint_receipt"] = None
        run["satisfied_evidence"] = [
            item
            for item in run["satisfied_evidence"]
            if item not in {"release_check_passed", "human_approval"}
        ]
        run.pop("approval", None)
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "attempt_id": attempt_id},
                acted={"event": "checkpoint_not_passed", "reason": reason},
                forward={"remaining": self.missing_requirements(run)},
            )
        except Exception:
            pass
        return run

    @_serialized_mutation
    def record_checkpoint_receipt(
        self, session_id: str, receipt: dict[str, Any], *, attempt_id: str
    ) -> dict[str, Any]:
        run = self.get_verified_run(session_id)
        if run["status"] != "open":
            raise MemoryIntegrityError("supervision run is not open")
        attempt = run.get("checkpoint_attempt")
        if not isinstance(attempt, dict) or attempt.get("attempt_id") != attempt_id:
            raise MemoryIntegrityError("checkpoint receipt belongs to a stale attempt")
        receipt = _checkpoint_receipt(receipt, run)
        run["checkpoint_attempt"] = None
        run["checkpoint_receipt"] = deepcopy(receipt)
        if "release_check_passed" not in run["satisfied_evidence"]:
            run["satisfied_evidence"].append("release_check_passed")
        run["satisfied_evidence"].sort()
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "receipt": receipt["digest"]},
                acted={"event": "checkpoint_passed", "exit_code": 0},
                forward={"remaining": self.missing_requirements(run)},
            )
        except Exception:
            pass
        return run

    @_serialized_mutation
    def approve(self, session_id: str, *, approved_at: str, signature: str) -> dict[str, Any]:
        run = self.get_verified_run(session_id)
        if run["status"] != "open":
            raise MemoryIntegrityError("supervision run is not open")
        if not run["lesson_ids"]:
            raise MemoryIntegrityError("no remembered intervention requires approval")
        lessons = [
            validate_lesson(self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body"))
            for lesson_id in run["lesson_ids"]
        ]
        closers = {lesson["authorized_closer"].lower() for lesson in lessons}
        if len(closers) != 1:
            raise MemoryIntegrityError("conflicting authorized closers require operator recovery")
        if "release_check_passed" in run["required_evidence"] and not run.get(
            "checkpoint_receipt"
        ):
            raise MemoryIntegrityError("approval requires a valid checkpoint receipt")
        try:
            approval_time = datetime.fromisoformat(approved_at)
        except ValueError as exc:
            raise MemoryIntegrityError("approval timestamp is invalid") from exc
        if approval_time.tzinfo is None or approval_time > datetime.now(timezone.utc) + _CLOCK_SKEW:
            raise MemoryIntegrityError("approval timestamp is invalid")
        receipt = run.get("checkpoint_receipt")
        if isinstance(receipt, dict) and approval_time < datetime.fromisoformat(
            receipt["completed_at"]
        ):
            raise MemoryIntegrityError("approval predates its checkpoint")
        recovered = recover_address(approval_message(run, approved_at), signature)
        if recovered not in closers:
            raise MemoryIntegrityError("approval is not signed by the authorized closer")
        run["approval"] = {"approved_at": approved_at, "signer": recovered, "signature": signature}
        if "human_approval" not in run["satisfied_evidence"]:
            run["satisfied_evidence"].append("human_approval")
        run["satisfied_evidence"].sort()
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "lesson_ids": run["lesson_ids"]},
                acted={"event": "human_approval", "signer": recovered},
                forward={"remaining": self.missing_requirements(run)},
            )
        except Exception:
            # The signed run entity is authoritative. Reporting a failure after
            # it commits would mislead the approving operator.
            pass
        return run

    @_serialized_mutation
    def begin_release(self, session_id: str, *, state_fingerprint: str) -> dict[str, Any]:
        run = self.get_verified_run(session_id)
        if run["status"] != "open":
            raise MemoryIntegrityError("release already started or supervision run is closed")
        missing = self.missing_requirements(run)
        if missing:
            raise MemoryIntegrityError(
                "release remains blocked; required evidence: " + ", ".join(missing)
            )
        receipt = run.get("checkpoint_receipt")
        checkpoint_required = "release_check_passed" in run["required_evidence"]
        if checkpoint_required and isinstance(receipt, dict):
            completed = datetime.fromisoformat(receipt["completed_at"])
            if datetime.now(timezone.utc) - completed > _MAX_CHECKPOINT_AGE:
                raise MemoryIntegrityError(
                    "checkpoint evidence is older than 15 minutes; run it again"
                )
        if checkpoint_required and (
            not isinstance(receipt, dict)
            or receipt.get("state_fingerprint") != state_fingerprint
        ):
            raise MemoryIntegrityError("repository state changed after the approved checkpoint")
        run["status"] = "executing"
        run["release_started_at"] = utc_now()
        run["updated_at"] = run["release_started_at"]
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="executing")
        try:
            self.client.write_event(
                evaluated={
                    "session_id": session_id,
                    "state_fingerprint": state_fingerprint,
                },
                acted={"event": "release_started"},
                forward={"lesson_ids": run["lesson_ids"]},
            )
        except Exception:
            # The executing entity is the safety-critical transition. Once it
            # is durable, a journal outage must not make the caller discard the
            # release lock or misclassify the transition as not begun.
            pass
        return run

    @staticmethod
    def missing_requirements(run: dict[str, Any]) -> list[str]:
        return sorted(set(run["required_evidence"]) - set(run["satisfied_evidence"]))

    @_serialized_mutation
    def record_release_outcome(self, session_id: str, *, success: bool) -> dict[str, Any]:
        run = self.get_run(session_id)
        if run["status"] != "executing":
            raise MemoryIntegrityError(
                "release outcome requires an executing one-shot capability"
            )
        desired = "success" if success else "failure"
        lessons = [
            validate_lesson(
                self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
            )
            for lesson_id in run["lesson_ids"]
        ]
        for lesson in lessons:
            if lesson["repo_id"] != self.repo_id:
                raise MemoryIntegrityError("release lesson belongs to another repository")
            prior = lesson["applied_release_outcomes"].get(session_id)
            if prior is not None and prior != desired:
                raise MemoryIntegrityError("release outcome conflicts with a prior application")
            if prior is None and lesson["revision"] != run["lesson_revisions"][lesson["lesson_id"]]:
                raise MemoryIntegrityError(
                    "intervention lesson changed while the release was executing"
                )
        for lesson in lessons:
            if lesson["applied_release_outcomes"].get(session_id) == desired:
                continue
            if success:
                lesson["success_count"] += 1
                lesson["probation_success_count"] += 1
            else:
                lesson["failure_count"] += 1
                lesson["probation_success_count"] = 0
            lesson["applied_release_outcomes"][session_id] = desired
            lesson["current_mode"] = _lesson_mode(
                probation_success_count=lesson["probation_success_count"],
                unresolved_release_count=lesson["unresolved_release_count"],
            )
            lesson["revision"] += 1
            lesson["updated_at"] = utc_now()
            self.client.set_entity(
                self.LESSON_CATEGORY,
                lesson["lesson_id"],
                lesson,
                status="active",
            )
        run["status"] = "completed" if success else "failed"
        run["outcome"] = "success" if success else "failure"
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status=run["status"])
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "mode": run["mode"]},
                acted={"event": "release_outcome", "success": success},
                forward={"lesson_ids": run["lesson_ids"]},
            )
        except Exception:
            pass
        return run

    @_serialized_mutation
    def record_release_unknown(self, session_id: str, *, reason: str) -> dict[str, Any]:
        run = self.get_run(session_id)
        if run["status"] != "executing":
            raise MemoryIntegrityError(
                "unknown release outcome requires an executing one-shot capability"
            )
        lessons = [
            validate_lesson(
                self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
            )
            for lesson_id in run["lesson_ids"]
        ]
        for lesson in lessons:
            if lesson["repo_id"] != self.repo_id:
                raise MemoryIntegrityError("release lesson belongs to another repository")
            prior = lesson["applied_release_outcomes"].get(session_id)
            if prior is not None and prior != "unknown":
                raise MemoryIntegrityError(
                    "unknown outcome conflicts with a prior terminal outcome"
                )
        for lesson in lessons:
            if lesson["applied_release_outcomes"].get(session_id) == "unknown":
                continue
            lesson["unresolved_release_count"] += 1
            lesson["probation_success_count"] = 0
            lesson["applied_release_outcomes"][session_id] = "unknown"
            lesson["current_mode"] = "HUMAN_REQUIRED"
            lesson["revision"] += 1
            lesson["updated_at"] = utc_now()
            self.client.set_entity(
                self.LESSON_CATEGORY,
                lesson["lesson_id"],
                lesson,
                status="active",
            )
        run["status"] = "unknown"
        run["outcome"] = "unknown"
        run["outcome_reason"] = reason
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="unknown")
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "mode": run["mode"]},
                acted={"event": "release_outcome_unknown", "reason": reason},
                forward={"lesson_ids": run["lesson_ids"]},
            )
        except Exception:
            pass
        return run

    @_serialized_mutation
    def reconcile_release(
        self,
        session_id: str,
        *,
        resolution: str,
        resolved_at: str,
        signature: str,
    ) -> dict[str, Any]:
        if resolution not in {"released", "not_released"}:
            raise MemoryIntegrityError("release resolution is unsupported")
        run = self.get_run(session_id)
        prior_status = run["status"]
        if run["status"] not in {"open", "executing", "unknown", "completed", "failed"}:
            raise MemoryIntegrityError(
                "only an open reserved, executing, unknown, or lock-retained terminal release can be reconciled"
            )
        if run.get("reconciliation") is not None:
            raise MemoryIntegrityError("release has already been reconciled")
        if run["status"] == "open" and resolution != "not_released":
            raise MemoryIntegrityError(
                "a reserved release that never started can only be reconciled as not_released"
            )
        if run["status"] == "completed" and resolution != "released":
            raise MemoryIntegrityError("completed release can only be reconciled as released")
        if run["status"] == "failed" and resolution != "not_released":
            raise MemoryIntegrityError("failed release can only be reconciled as not_released")
        try:
            resolved = datetime.fromisoformat(resolved_at)
        except ValueError as exc:
            raise MemoryIntegrityError("reconciliation timestamp is invalid") from exc
        if (
            resolved.tzinfo is None
            or resolved > datetime.now(timezone.utc) + _CLOCK_SKEW
        ):
            raise MemoryIntegrityError("reconciliation timestamp is invalid")
        chronology_field = "started_at" if prior_status == "open" else "release_started_at"
        try:
            chronology_start = datetime.fromisoformat(
                str(run.get(chronology_field, ""))
            )
        except ValueError as exc:
            raise MemoryIntegrityError("reconciliation chronology is invalid") from exc
        if chronology_start.tzinfo is None or resolved < chronology_start:
            raise MemoryIntegrityError("reconciliation chronology is invalid")

        signed_reconciliation_fields = reconciliation_fields(
            run, resolution, resolved_at
        )

        lessons = [
            validate_lesson(
                self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body")
            )
            for lesson_id in run["lesson_ids"]
        ]
        if not lessons or any(lesson["repo_id"] != self.repo_id for lesson in lessons):
            raise MemoryIntegrityError("release reconciliation has no valid repository lesson")
        closers = {lesson["authorized_closer"].lower() for lesson in lessons}
        if len(closers) != 1:
            raise MemoryIntegrityError("conflicting authorized closers require operator recovery")
        recovered = recover_address(
            reconciliation_message_from_fields(signed_reconciliation_fields), signature
        )
        if recovered not in closers:
            raise MemoryIntegrityError(
                "reconciliation is not signed by the authorized closer"
            )

        for lesson in lessons:
            if prior_status == "open":
                # The on-disk reservation was created but begin_release never
                # became durable. Closing that orphan must not count as either
                # a successful or failed deployment.
                continue
            desired = "success" if resolution == "released" else "failure"
            prior = lesson["applied_release_outcomes"].get(session_id)
            if prior not in {None, "unknown", desired}:
                raise MemoryIntegrityError(
                    "reconciliation conflicts with a prior release outcome"
                )
            changed = False
            can_credit_terminal_outcome = False
            if prior == "unknown":
                if lesson["unresolved_release_count"] < 1:
                    raise MemoryIntegrityError(
                        "unknown release has no corresponding unresolved lesson debt"
                    )
                lesson["unresolved_release_count"] -= 1
                changed = True
                # UNKNOWN itself advances exactly one revision. Credit the
                # reconciled terminal result only when no later intervention or
                # outcome changed this lesson in the meantime.
                can_credit_terminal_outcome = (
                    lesson["revision"]
                    == run["lesson_revisions"].get(lesson["lesson_id"], -2) + 1
                )
            elif prior is None and prior_status == "executing":
                can_credit_terminal_outcome = (
                    lesson["revision"]
                    == run["lesson_revisions"].get(lesson["lesson_id"])
                )
            if can_credit_terminal_outcome:
                if resolution == "released":
                    lesson["success_count"] += 1
                    lesson["probation_success_count"] += 1
                else:
                    lesson["failure_count"] += 1
                    lesson["probation_success_count"] = 0
                changed = True
            if prior != desired:
                lesson["applied_release_outcomes"][session_id] = desired
                changed = True
            lesson["current_mode"] = _lesson_mode(
                probation_success_count=lesson["probation_success_count"],
                unresolved_release_count=lesson["unresolved_release_count"],
            )
            if changed:
                lesson["revision"] += 1
                lesson["updated_at"] = utc_now()
                self.client.set_entity(
                    self.LESSON_CATEGORY,
                    lesson["lesson_id"],
                    lesson,
                    status="active",
                )

        run["status"] = "completed" if resolution == "released" else "failed"
        run["outcome"] = "success" if resolution == "released" else "failure"
        if prior_status == "open":
            run["outcome_reason"] = "reserved_release_never_started"
        run["reconciliation"] = {
            "resolution": resolution,
            "resolved_at": resolved_at,
            "signer": recovered,
            "signature": signature,
            "signed_fields": signed_reconciliation_fields,
        }
        run["updated_at"] = utc_now()
        self.client.set_entity(
            self.RUN_CATEGORY,
            session_id,
            run,
            status=run["status"],
        )
        try:
            self.client.write_event(
                evaluated={"session_id": session_id, "prior_status": prior_status},
                acted={
                    "event": "release_reconciled",
                    "resolution": resolution,
                    "signer": recovered,
                },
                forward={"lesson_ids": run["lesson_ids"]},
            )
        except Exception:
            # The signed run entity is the authoritative recovery record. A
            # secondary journal outage must not strand its retained lock.
            pass
        return run
