from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import NotFoundError

from .identity import tenant_id
from .policy import MODES, mode_for_outcomes, requirements_for_mode
from .signing import approval_message, intervention_message, recover_address


class MemoryIntegrityError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MemoryIntegrityError(f"{field} must be a string list")
    return list(value)


def validate_lesson(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise MemoryIntegrityError("lesson body is corrupted")
    required_strings = (
        "lesson_id", "repo_id", "task_class", "area", "agent_family",
        "severity", "checkpoint_command", "current_mode", "authorized_closer", "source_session_id",
        "incident_at", "updated_at", "status",
    )
    for field in required_strings:
        if not isinstance(body.get(field), str) or not body[field]:
            raise MemoryIntegrityError(f"lesson {field} is invalid")
    if body["current_mode"] not in MODES:
        raise MemoryIntegrityError("lesson mode is invalid")
    if body["status"] != "active":
        raise MemoryIntegrityError("lesson is not active")
    for field in ("failure_count", "success_count", "revision"):
        if not isinstance(body.get(field), int) or body[field] < 0:
            raise MemoryIntegrityError(f"lesson {field} is invalid")
    body = deepcopy(body)
    body["required_evidence"] = _strings(body.get("required_evidence"), "required_evidence")
    signature = body.get("intervention_signature")
    signed_fields = body.get("signed_fields")
    if not isinstance(signature, str) or not isinstance(signed_fields, dict):
        raise MemoryIntegrityError("lesson lacks signed intervention provenance")
    signer = recover_address(intervention_message(signed_fields), signature)
    if signer != body["authorized_closer"].lower():
        raise MemoryIntegrityError("lesson intervention signature is invalid")
    for key, expected in signed_fields.items():
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
    run = deepcopy(body)
    if not isinstance(run.get("checkpoint_command"), str):
        raise MemoryIntegrityError("run checkpoint_command is invalid")
    run["lesson_ids"] = _strings(run.get("lesson_ids"), "lesson_ids")
    run["required_evidence"] = _strings(run.get("required_evidence"), "required_evidence")
    run["satisfied_evidence"] = _strings(run.get("satisfied_evidence"), "satisfied_evidence")
    return run


class InterventionMemory:
    LESSON_CATEGORY = "intervention_lesson"
    RUN_CATEGORY = "supervision_run"

    def __init__(self, db_path: str | Path, repo_id: str) -> None:
        self.repo_id = repo_id
        self.client = MemoryClient.local(db_path, tenant_id=tenant_id(repo_id))

    def record_intervention(self, record: dict[str, Any]) -> dict[str, Any]:
        signed_fields = record.get("signed_fields")
        signature = record.get("intervention_signature")
        if not isinstance(signed_fields, dict) or not isinstance(signature, str):
            raise MemoryIntegrityError("signed intervention fields are required")
        if signed_fields.get("repo_id") != self.repo_id:
            raise MemoryIntegrityError("intervention belongs to another repository")
        closer = str(signed_fields.get("authorized_closer", "")).lower()
        if recover_address(intervention_message(signed_fields), signature) != closer:
            raise MemoryIntegrityError("intervention signature is invalid")
        lesson_id = str(signed_fields["lesson_id"])
        failures = 1
        successes = 0
        revision = 1
        try:
            prior = validate_lesson(self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body"))
            failures = prior["failure_count"] + 1
            successes = prior["success_count"]
            revision = prior["revision"] + 1
        except NotFoundError:
            pass
        body = {
            **signed_fields,
            "authorized_closer": closer,
            "intervention_signature": signature,
            "signed_fields": deepcopy(signed_fields),
            "incident_summary": str(record.get("incident_summary", "")),
            "failure_count": failures,
            "success_count": successes,
            "current_mode": mode_for_outcomes(failures, successes),
            "revision": revision,
            "updated_at": utc_now(),
            "status": "active",
        }
        validated = validate_lesson(body)
        self.client.set_entity(self.LESSON_CATEGORY, lesson_id, validated, status="active")
        self.client.write_event(
            evaluated={"repo_id": self.repo_id, "task_class": validated["task_class"]},
            acted={"event": "human_intervention", "lesson_id": lesson_id},
            forward={"mode": validated["current_mode"]},
        )
        return validated

    def matching_lessons(self, task_class: str, area: str, agent_family: str) -> list[dict[str, Any]]:
        matches = []
        for entity in self.client.list_entities(self.LESSON_CATEGORY, status="active", limit=100):
            lesson = validate_lesson(entity.get("body"))
            if (
                lesson["repo_id"] == self.repo_id
                and lesson["task_class"] == task_class
                and lesson["area"] == area
                and lesson["agent_family"].lower() == agent_family.lower()
            ):
                matches.append(lesson)
        return matches

    def start_run(
        self,
        *,
        session_id: str,
        task_class: str,
        area: str,
        agent_family: str,
        model: str,
        process_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            existing = self.get_run(session_id)
        except MemoryIntegrityError as exc:
            if "no Sibyl supervision run exists" not in str(exc):
                raise
        else:
            if existing["status"] == "open":
                return existing
        lessons = self.matching_lessons(task_class, area, agent_family)
        checkpoint_commands = {lesson["checkpoint_command"].strip() for lesson in lessons}
        if len(checkpoint_commands) > 1:
            raise MemoryIntegrityError("conflicting remembered checkpoint commands require operator resolution")
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
            "checkpoint_command": next(iter(checkpoint_commands), ""),
            "required_evidence": requirements_for_mode(mode),
            "satisfied_evidence": [],
            "status": "open",
            "started_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        self.client.write_event(
            evaluated={"session_id": session_id, "task_class": task_class, "lesson_ids": run["lesson_ids"]},
            acted={"event": "supervision_selected", "mode": mode},
            forward={"required_evidence": run["required_evidence"]},
        )
        return run

    def get_run(self, session_id: str) -> dict[str, Any]:
        try:
            entity = self.client.get_entity(self.RUN_CATEGORY, session_id)
        except NotFoundError as exc:
            raise MemoryIntegrityError("no Sibyl supervision run exists for this session") from exc
        return validate_run(entity.get("body"))

    def add_evidence(self, session_id: str, evidence: str) -> dict[str, Any]:
        run = self.get_run(session_id)
        if run["status"] != "open":
            return run
        if evidence not in run["satisfied_evidence"]:
            run["satisfied_evidence"].append(evidence)
        run["satisfied_evidence"].sort()
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        return run

    def approve(self, session_id: str, *, approved_at: str, signature: str) -> dict[str, Any]:
        run = self.get_run(session_id)
        if not run["lesson_ids"]:
            raise MemoryIntegrityError("no remembered intervention requires approval")
        lessons = [
            validate_lesson(self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body"))
            for lesson_id in run["lesson_ids"]
        ]
        closers = {lesson["authorized_closer"].lower() for lesson in lessons}
        if len(closers) != 1:
            raise MemoryIntegrityError("conflicting authorized closers require operator recovery")
        recovered = recover_address(approval_message(run, approved_at), signature)
        if recovered not in closers:
            raise MemoryIntegrityError("approval is not signed by the authorized closer")
        run["approval"] = {"approved_at": approved_at, "signer": recovered, "signature": signature}
        if "human_approval" not in run["satisfied_evidence"]:
            run["satisfied_evidence"].append("human_approval")
        run["satisfied_evidence"].sort()
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status="open")
        self.client.write_event(
            evaluated={"session_id": session_id, "lesson_ids": run["lesson_ids"]},
            acted={"event": "human_approval", "signer": recovered},
            forward={"remaining": self.missing_requirements(run)},
        )
        return run

    @staticmethod
    def missing_requirements(run: dict[str, Any]) -> list[str]:
        return sorted(set(run["required_evidence"]) - set(run["satisfied_evidence"]))

    def record_release_outcome(self, session_id: str, *, success: bool) -> dict[str, Any]:
        run = self.get_run(session_id)
        if run["status"] != "open":
            return run
        for lesson_id in run["lesson_ids"]:
            lesson = validate_lesson(self.client.get_entity(self.LESSON_CATEGORY, lesson_id).get("body"))
            if success:
                lesson["success_count"] += 1
            else:
                lesson["failure_count"] += 1
            lesson["current_mode"] = mode_for_outcomes(lesson["failure_count"], lesson["success_count"])
            lesson["revision"] += 1
            lesson["updated_at"] = utc_now()
            self.client.set_entity(self.LESSON_CATEGORY, lesson_id, lesson, status="active")
        run["status"] = "completed" if success else "failed"
        run["outcome"] = "success" if success else "failure"
        run["updated_at"] = utc_now()
        self.client.set_entity(self.RUN_CATEGORY, session_id, run, status=run["status"])
        self.client.write_event(
            evaluated={"session_id": session_id, "mode": run["mode"]},
            acted={"event": "release_outcome", "success": success},
            forward={"lesson_ids": run["lesson_ids"]},
        )
        return run
