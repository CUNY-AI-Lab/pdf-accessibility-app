"""The runner's calls to the Worker's runner API.

Every request carries the host credential in ``x-pdf-runner-token``; calls
about one task also carry that task's lease in ``x-pdf-job-token``. Doorway
strips ``Authorization`` on the way in, so neither travels there.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.services.llm_client import ModelEndpoint

logger = logging.getLogger(__name__)

RUNNER_TOKEN_HEADER = "x-pdf-runner-token"
JOB_TOKEN_HEADER = "x-pdf-job-token"
TASK_KINDS = frozenset({"process", "review", "sync"})
UPLOAD_ATTEMPTS = 3


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

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Task":
        kind = str(data["kind"])
        if kind not in TASK_KINDS:
            raise ValueError(f"unknown task kind {kind!r}")
        action = data.get("action")
        if kind == "review" and not isinstance(action, dict):
            raise ValueError("a review task needs an action")
        overrides = data.get("overrides") or {}
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
        )


class WorkerClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
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
        )

    async def claim(self, runner_id: str) -> Task | None:
        response = await self._client.post("/claim", json={"runner": runner_id})
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return Task.from_json(response.json()["task"])

    async def download(self, task: Task, what: str, dest: Path) -> None:
        """Stream the task's input PDF or saved state to ``dest``."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._client.stream(
            "GET", f"/tasks/{task.job_id}/{what}", headers=self._task_headers(task)
        ) as response:
            _raise_for_task(response)
            with open(dest, "wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    out.write(chunk)

    async def progress(self, task: Task, event: dict[str, Any]) -> None:
        """Report a step event; an empty event only renews the lease."""
        response = await self._client.post(
            f"/tasks/{task.job_id}/progress", json=event, headers=self._task_headers(task)
        )
        _raise_for_task(response)

    async def upload(self, task: Task, name: str, body: bytes | Path, content_type: str) -> None:
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                content = body.read_bytes() if isinstance(body, Path) else body
                response = await self._client.put(
                    f"/tasks/{task.job_id}/files/{name}",
                    content=content,
                    headers={**self._task_headers(task), "content-type": content_type},
                )
                _raise_for_task(response)
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == UPLOAD_ATTEMPTS:
                    raise
                logger.warning("Upload of %s failed (%s); retrying", name, exc)
                await asyncio.sleep(2**attempt)

    async def complete(self, task: Task, payload: dict[str, Any]) -> None:
        response = await self._client.post(
            f"/tasks/{task.job_id}/complete", json=payload, headers=self._task_headers(task)
        )
        _raise_for_task(response)

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
