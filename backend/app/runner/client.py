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
UPLOAD_ATTEMPTS = 3
RELEASE_TIMEOUT_SECONDS = 5.0


class UploadPhase(Enum):
    SENDING = "sending"
    BACKOFF = "backoff"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


UPLOAD_TRANSITIONS = {
    UploadPhase.SENDING: {
        UploadPhase.BACKOFF,
        UploadPhase.ACKNOWLEDGED,
        UploadPhase.FAILED,
        UploadPhase.LEASE_LOST,
    },
    UploadPhase.BACKOFF: {UploadPhase.SENDING, UploadPhase.FAILED},
    UploadPhase.ACKNOWLEDGED: set(),
    UploadPhase.FAILED: set(),
    UploadPhase.LEASE_LOST: set(),
}


def upload_transition(current: UploadPhase, target: UploadPhase) -> UploadPhase:
    if target not in UPLOAD_TRANSITIONS[current]:
        raise ValueError(f"Illegal upload transition: {current.value} -> {target.value}")
    return target


def retryable_upload(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
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
        # Monotonic time of the Worker's last real answer, for the health check.
        self.last_contact: float | None = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={RUNNER_TOKEN_HEADER: token},
            timeout=httpx.Timeout(120.0, connect=15.0),
            transport=transport,
            event_hooks={"response": [self._note_contact]},
        )

    async def _note_contact(self, response: httpx.Response) -> None:
        # A refused credential or a failing Worker is not contact: the runner
        # can't take work through either. A 404/409/410 about one lease is.
        if response.status_code < 500 and response.status_code not in {401, 403}:
            self.last_contact = time.monotonic()

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

        async def fetch() -> None:
            async with self._client.stream(
                "GET", f"/tasks/{task.job_id}/{what}", headers=self._task_headers(task)
            ) as response:
                _raise_for_task(response)
                # "wb" so a retry starts the file over.
                with open(dest, "wb") as out:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        out.write(chunk)

        await self._with_retries(task, f"Download of {what}", fetch)

    async def progress(self, task: Task, event: dict[str, Any]) -> None:
        """Report a step event; an empty event only renews the lease."""
        async def send() -> None:
            response = await self._client.post(
                f"/tasks/{task.job_id}/progress", json=event, headers=self._task_headers(task)
            )
            _raise_for_task(response)

        await self._with_retries(task, "Progress", send)

    async def upload(self, task: Task, name: str, body: bytes | Path, content_type: str) -> None:
        phase = UploadPhase.SENDING
        # Every retry sends exactly the same bytes, even if a caller's file changes.
        content = body.read_bytes() if isinstance(body, Path) else body
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            remaining = task.deadline_ms / 1000 - time.time()
            if remaining <= 0:
                upload_transition(phase, UploadPhase.FAILED)
                raise TimeoutError("Upload deadline passed")
            try:
                async with asyncio.timeout(remaining):
                    response = await self._client.put(
                        f"/tasks/{task.job_id}/files/{name}",
                        content=content,
                        headers={**self._task_headers(task), "content-type": content_type},
                    )
                _raise_for_task(response)
                upload_transition(phase, UploadPhase.ACKNOWLEDGED)
                return
            except TimeoutError:
                upload_transition(phase, UploadPhase.FAILED)
                raise
            except LeaseLost:
                upload_transition(phase, UploadPhase.LEASE_LOST)
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == UPLOAD_ATTEMPTS or not retryable_upload(exc):
                    upload_transition(phase, UploadPhase.FAILED)
                    raise
                phase = upload_transition(phase, UploadPhase.BACKOFF)
                delay = 2**attempt
                remaining = task.deadline_ms / 1000 - time.time()
                if delay >= remaining:
                    upload_transition(phase, UploadPhase.FAILED)
                    raise TimeoutError("Upload retry would exceed deadline") from exc
                logger.warning("Upload of %s failed; retrying", name)
                await asyncio.sleep(delay)
                phase = upload_transition(phase, UploadPhase.SENDING)

    async def complete(self, task: Task, payload: dict[str, Any]) -> None:
        # The Worker keeps a receipt per lease, so a replayed completion whose
        # first answer was lost is acknowledged rather than refused.
        async def send() -> None:
            response = await self._client.post(
                f"/tasks/{task.job_id}/complete", json=payload, headers=self._task_headers(task)
            )
            _raise_for_task(response)

        await self._with_retries(task, "Completion", send)

    async def fail(self, task: Task, error: str) -> None:
        async def send() -> None:
            response = await self._client.post(
                f"/tasks/{task.job_id}/fail",
                json={"error": error},
                headers=self._task_headers(task),
            )
            _raise_for_task(response)

        # A task that ran out of time still reports it, so no deadline here.
        await self._with_retries(task, "Failure report", send, deadline=False)

    async def release(self, task: Task) -> None:
        """Hand a task back to the Worker's queue when the runner shuts down.

        One quick attempt: the process is about to be killed, and an
        unreleased task is still recovered when its lease lapses.
        """
        async with asyncio.timeout(RELEASE_TIMEOUT_SECONDS):
            response = await self._client.post(
                f"/tasks/{task.job_id}/release", headers=self._task_headers(task)
            )
        _raise_for_task(response)

    async def _with_retries(
        self,
        task: Task,
        label: str,
        send: Callable[[], Awaitable[None]],
        *,
        deadline: bool = True,
    ) -> None:
        """Send a call to the Worker, retrying transient failures like ``upload`` does."""
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                await send()
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == UPLOAD_ATTEMPTS or not retryable_upload(exc):
                    raise
                delay = 2**attempt
                if deadline and delay >= task.deadline_ms / 1000 - time.time():
                    raise TimeoutError(f"{label} retry would exceed deadline") from exc
                logger.warning(
                    "%s for job %s failed (%s); retrying", label, task.job_id, exc
                )
                await asyncio.sleep(delay)

    def _task_headers(self, task: Task) -> dict[str, str]:
        return {JOB_TOKEN_HEADER: task.lease}


def _raise_for_task(response: httpx.Response) -> None:
    if response.status_code in {404, 409, 410}:
        raise LeaseLost(f"{response.status_code} from the Worker")
    response.raise_for_status()
