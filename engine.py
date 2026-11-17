"""A small workflow engine that keeps its place through failures, restarts and approvals.

It runs steps in order, retries timeouts, pauses before any step that needs a
person's approval, and saves the workflow to a JSON file after every event.
Because the state lives on disk, a workflow can stop (to wait for approval,
after a failure, or in a crash) and a later command continues from the first
step that hasn't finished.
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class Step:
    name: str                           # key for this step's result in the shared data
    title: str                          # shown in the history, e.g. "Check availability"
    action: Callable[[dict], Any]       # gets the shared data, returns this step's result
    success_message: str                # shown in the history when the step succeeds
    max_attempts: int = 1               # how many tries a timing-out step gets
    approval_of: Optional[str] = None   # data a person must approve before this step runs


class WorkflowError(Exception):
    """A command that doesn't fit the workflow's current status, such as approving twice."""


def fingerprint(value: Any) -> str:
    """Short hash of a value. An approval stores it, so it covers only that exact draft."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]


def format_event(event: str, message: str) -> str:
    return f"{'[' + event + ']':<13}{message}"


class Engine:
    def __init__(self, steps: List[Step], state_dir, backoff_seconds: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 out: Callable[[str], None] = print):
        self.steps = steps
        self.state_dir = Path(state_dir)
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep  # replaceable so tests don't wait
        self.out = out      # replaceable so tests can capture the output

    # ---- Commands ----

    def run(self, workflow_id: str, inputs: Optional[dict] = None) -> dict:
        """Start a new workflow, or continue one that failed or was interrupted."""
        if not self._path(workflow_id).exists():
            state = {"id": workflow_id, "status": "RUNNING", "data": dict(inputs or {}),
                     "completed": [], "approvals": {}, "history": []}
            self._log(state, "START", f"Workflow {workflow_id}")
            return self._advance(state)

        state = self.load(workflow_id)
        if state["status"] == "PAUSED":
            raise WorkflowError(f"{workflow_id} is waiting for approval; approve or reject it")
        if state["status"] in ("COMPLETE", "REJECTED"):
            raise WorkflowError(f"{workflow_id} is already {state['status']}")
        # FAILED, or still RUNNING because the last process died partway through.
        state["status"] = "RUNNING"
        step = self._next_step(state)
        self._log(state, "RESUMED", f"Continuing from {step.title}" if step else "Continuing")
        return self._advance(state)

    def approve(self, workflow_id: str, by: str) -> dict:
        state, step = self._paused(workflow_id)
        draft = state["data"].get(step.approval_of)
        if draft is None:
            raise WorkflowError(f"{workflow_id} has nothing to approve yet")
        state["approvals"][step.name] = {"by": by, "at": _now(), "fingerprint": fingerprint(draft)}
        state["status"] = "RUNNING"
        self._log(state, "APPROVED", f"Human approves draft (by {by})")
        return self._advance(state)

    def reject(self, workflow_id: str, by: str, reason: str) -> dict:
        state, _ = self._paused(workflow_id)
        state["status"] = "REJECTED"
        self._log(state, "REJECTED", f"Human rejects draft (by {by}): {reason}")
        return state

    def load(self, workflow_id: str) -> dict:
        path = self._path(workflow_id)
        if not path.exists():
            raise WorkflowError(f"No workflow named {workflow_id}")
        return json.loads(path.read_text())

    # ---- Internals ----

    def _advance(self, state: dict) -> dict:
        """Run unfinished steps in order until the workflow pauses, fails or finishes."""
        for step in self.steps:
            if step.name in state["completed"]:
                continue
            if step.approval_of and not self._is_approved(state, step):
                state["status"] = "PAUSED"
                self._log(state, "PAUSED", "Waiting for human approval")
                return state
            if step.approval_of:
                # An approval covers one try. It's used up before the step runs (the RUNNING
                # log saves it), so a send that fails or is cut off can't repeat on its own.
                state["approvals"][step.name]["used"] = True
            if not self._run_step(state, step):
                state["status"] = "FAILED"
                self._log(state, "STOPPED", f"Workflow stopped at {step.title}")
                return state
        state["status"] = "COMPLETE"
        self._log(state, "COMPLETE", "Workflow finished")
        return state

    def _run_step(self, state: dict, step: Step) -> bool:
        self._log(state, "RUNNING", step.title)
        for attempt in range(1, step.max_attempts + 1):
            if attempt > 1:
                self.sleep(self.backoff_seconds * 2 ** (attempt - 2))  # 0.5s, then 1s
                self._log(state, "RETRY", f"Attempt {attempt}")
            try:
                result = step.action(state["data"])
            except TimeoutError as err:   # temporary, so another try may work
                self._log(state, "FAILED", str(err) or "Timed out")
                continue
            except Exception as err:      # anything else would fail the same way again
                self._log(state, "FAILED", f"{err} (not retried)")
                return False
            state["data"][step.name] = result
            state["completed"].append(step.name)
            self._log(state, "SUCCESS", step.success_message)
            return True
        return False

    def _is_approved(self, state: dict, step: Step) -> bool:
        approval = state["approvals"].get(step.name)
        draft = state["data"].get(step.approval_of)
        return (approval is not None and draft is not None and not approval.get("used")
                and approval["fingerprint"] == fingerprint(draft))

    def _paused(self, workflow_id: str) -> Tuple[dict, Step]:
        state = self.load(workflow_id)
        if state["status"] != "PAUSED":
            raise WorkflowError(
                f"{workflow_id} is not waiting for approval (status: {state['status']})")
        return state, self._next_step(state)

    def _next_step(self, state: dict) -> Optional[Step]:
        return next((s for s in self.steps if s.name not in state["completed"]), None)

    def _log(self, state: dict, event: str, message: str) -> None:
        """Add an event to the history and save before printing, so the screen
        never shows anything the state file doesn't have."""
        state["history"].append({"at": _now(), "event": event, "message": message})
        self._save(state)
        self.out(format_event(event, message))

    def _save(self, state: dict) -> None:
        """Write a temp file, then rename it over the old one. The rename is atomic,
        so a crash mid-write leaves the previous version, never half a file."""
        path = self._path(state["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, path)

    def _path(self, workflow_id: str) -> Path:
        # The ID becomes a file name, so allow only plain characters (no "../").
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow_id):
            raise WorkflowError(f"Invalid workflow ID: {workflow_id!r}")
        return self.state_dir / f"{workflow_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
