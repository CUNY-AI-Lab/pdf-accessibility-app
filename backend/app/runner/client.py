"""The runner's calls to the Worker's runner API.

Every request carries the host credential in ``x-pdf-runner-token``; calls
about one task also carry that task's lease in ``x-pdf-job-token``. Doorway
strips ``Authorization`` on the way in, so neither travels there.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from app.services.llm_client import ModelEndpoint

logger = logging.getLogger(__name__)

RUNNER_TOKEN_HEADER = "x-pdf-runner-token"
JOB_TOKEN_HEADER = "x-pdf-job-token"
TASK_KINDS = frozenset({"process", "review", "sync"})
# R2 errors and dropped connections can last 15 s (issue #90). Five tries,
# 2, 4, 8 and 16 s apart, ride out half a minute; the task deadline still wins.
TRANSFER_ATTEMPTS = 5
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransferPhase(Enum):
    SENDING = "sending"
    BACKOFF = "backoff"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


TRANSFER_TRANSITIONS = {
    TransferPhase.SENDING: {
        TransferPhase.BACKOFF,
        TransferPhase.ACKNOWLEDGED,
        TransferPhase.FAILED,
        TransferPhase.LEASE_LOST,
    },
    TransferPhase.BACKOFF: {TransferPhase.SENDING, TransferPhase.FAILED},
    TransferPhase.ACKNOWLEDGED: set(),
    TransferPhase.FAILED: set(),
    TransferPhase.LEASE_LOST: set(),
}


def transfer_transition(current: TransferPhase, target: TransferPhase) -> TransferPhase:
    if target not in TRANSFER_TRANSITIONS[current]:
        raise ValueError(f"Illegal transfer transition: {current.value} -> {target.value}")
    return target


def retryable_transfer(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUSES
    )


class LeaseLost(Exception):
    """The Worker no longer holds this task for the runner.

    The job was deleted, its lease or deadline passed, or the person's
    credential ended; the runner stops work on it.
    """


@dataclass(frozen=True)
class Task:
    job_id: str
    kind: str
    lease: str
    attempt: int
    deadline_ms: int
    original_filename: str
    file_size_bytes: int | None = None
    action: dict[str, Any] | None = None
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    overrides_version: int = 0
    attempt_id: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Task":
        for key in ("job_id", "kind", "lease", "original_filename"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"Claim requires a nonempty {key}")
        for key in ("attempt", "deadline"):
            if type(data.get(key)) is not int or data[key] <= 0:
                raise ValueError(f"Claim requires a positive {key}")
        kind = str(data["kind"])
        if kind not in TASK_KINDS:
            raise ValueError(f"unknown task kind {kind!r}")
        action = data.get("action")
        if kind == "review" and not isinstance(action, dict):
            raise ValueError("a review task needs an action")
        overrides = data.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("Claim overrides must be an object")
        return cls(
            job_id=str(data["job_id"]),
            kind=kind,
            lease=str(data["lease"]),
            attempt=int(data["attempt"]),
            deadline_ms=int(data["deadline"]),
            original_filename=str(data["original_filename"]),
            file_size_bytes=data.get("file_size_bytes"),
            action=action if isinstance(action, dict) else None,
            overrides={
                "changes": dict(overrides.get("changes") or {}),
                "tasks": dict(overrides.get("tasks") or {}),
            },
            overrides_version=int(data.get("overrides_version") or 0),
            attempt_id=data.get("attempt_id"),
        )


class WorkerClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={RUNNER_TOKEN_HEADER: token},
            timeout=httpx.Timeout(120.0, connect=15.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def model_endpoint(self, task: Task) -> ModelEndpoint:
        """The Worker's relay for this task's model calls."""
        return ModelEndpoint(
            base_url=f"{self.base_url}/tasks/{task.job_id}/v1",
            headers={RUNNER_TOKEN_HEADER: self._token, JOB_TOKEN_HEADER: task.lease},
            # Every task's relay reaches the same Gateway.
            capability_scope=f"{self.base_url}/v1",
        )

    async def claim(self, runner_id: str) -> Task | None:
        response = await self._client.post("/claim", json={"runner": runner_id})
        if response.status_code == 204:
            return None
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("task"), dict):
            raise ValueError("Claim response requires a task object")
        return Task.from_json(body["task"])

    async def download(self, task: Task, what: str, dest: Path) -> None:
        """Stream the task's input PDF or saved state to ``dest``."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        async def receive() -> None:
            async with self._client.stream(
                "GET", f"/tasks/{task.job_id}/{what}", headers=self._task_headers(task)
            ) as response:
                _raise_for_task(response)
                # Every try writes the file from its first byte.
                with open(dest, "wb") as out:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        out.write(chunk)

        await self._transfer(task, f"Download of {what}", receive)

    async def progress(self, task: Task, event: dict[str, Any]) -> None:
        """Report a step event; an empty event only renews the lease."""
        response = await self._client.post(
            f"/tasks/{task.job_id}/progress", json=event, headers=self._task_headers(task)
        )
        _raise_for_task(response)

    async def upload(self, task: Task, name: str, body: bytes | Path, content_type: str) -> None:
        # Every retry sends exactly the same bytes, even if a caller's file changes.
        content = body.read_bytes() if isinstance(body, Path) else body

        async def send() -> None:
            response = await self._client.put(
                f"/tasks/{task.job_id}/files/{name}",
                content=content,
                headers={**self._task_headers(task), "content-type": content_type},
            )
            _raise_for_task(response)

        await self._transfer(task, f"Upload of {name}", send)

    async def _transfer(
        self, task: Task, label: str, attempt_once: Callable[[], Awaitable[None]]
    ) -> None:
        """Retry transient storage and network failures within the task's deadline.

        A lost lease or a refused request ends the transfer at once.
        """
        phase = TransferPhase.SENDING
        for attempt in range(1, TRANSFER_ATTEMPTS + 1):
            remaining = task.deadline_ms / 1000 - time.time()
            if remaining <= 0:
                transfer_transition(phase, TransferPhase.FAILED)
                raise TimeoutError(f"{label} deadline passed")
            try:
                async with asyncio.timeout(remaining):
                    await attempt_once()
                transfer_transition(phase, TransferPhase.ACKNOWLEDGED)
                return
            except TimeoutError:
                transfer_transition(phase, TransferPhase.FAILED)
                raise
            except LeaseLost:
                transfer_transition(phase, TransferPhase.LEASE_LOST)
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == TRANSFER_ATTEMPTS or not retryable_transfer(exc):
                    transfer_transition(phase, TransferPhase.FAILED)
                    raise
                phase = transfer_transition(phase, TransferPhase.BACKOFF)
                delay = 2**attempt
                remaining = task.deadline_ms / 1000 - time.time()
                if delay >= remaining:
                    transfer_transition(phase, TransferPhase.FAILED)
                    raise TimeoutError(f"{label} retry would exceed deadline") from exc
                logger.warning(
                    "%s for job %s failed; retrying in %d s: %s", label, task.job_id, delay, exc
                )
                await asyncio.sleep(delay)
                phase = transfer_transition(phase, TransferPhase.SENDING)

    async def complete(self, task: Task, payload: dict[str, Any]) -> None:
        # The Worker keeps one receipt per lease: repeating a completion that
        # already landed is acknowledged again, not applied twice.
        async def send() -> None:
            response = await self._client.post(
                f"/tasks/{task.job_id}/complete", json=payload, headers=self._task_headers(task)
            )
            _raise_for_task(response)

        await self._transfer(task, "Completion", send)

    async def fail(self, task: Task, error: str) -> None:
        response = await self._client.post(
            f"/tasks/{task.job_id}/fail", json={"error": error}, headers=self._task_headers(task)
        )
        _raise_for_task(response)

    def _task_headers(self, task: Task) -> dict[str, str]:
        return {JOB_TOKEN_HEADER: task.lease}


def _raise_for_task(response: httpx.Response) -> None:
    if response.status_code in {404, 409, 410}:
        raise LeaseLost(f"{response.status_code} from the Worker")
    response.raise_for_status()
