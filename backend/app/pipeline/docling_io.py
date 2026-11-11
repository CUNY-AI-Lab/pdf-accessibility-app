"""Bounded Docling I/O on the v2 runner.

Remote transitions (one instance per conversion)::
    ready -> submitting
    submitting/resubmitting -> polling | unknown_submission | failed | timed_out | cancelled
    unknown_submission -> resubmitting (budget left) | timed_out | cancelled
    polling -> fetching | failed | timed_out | cancelled
    fetching -> succeeded | failed | timed_out | cancelled

Submission attempts, including the first POST, are capped at three. Uncertain
acceptance is safe to retry: duplicate conversions only cost local GPU time.
An exhausted attempt budget leaves unknown_submission terminal; deadline expiry
is timed_out instead. One deadline spans POSTs, backoff, polling and fetching.
Unknown remote statuses fail; a claimed instance cannot be reused concurrently.

Local transitions::
    ready -> original -> succeeded | repairing
    repairing -> repaired | failed
    repaired -> succeeded | failed

Local conversion makes at most one repair and one repaired-input attempt.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

DOCLING_SERVE_REQUEST_ATTEMPTS = 3
DOCLING_SERVE_RETRY_DELAY_SECONDS = 2.0
DOCLING_SERVE_RETRY_STATUS_CODES = {502, 503, 504}


class DoclingPhase(Enum):
    READY = "ready"
    SUBMITTING = "submitting"
    UNKNOWN_SUBMISSION = "unknown_submission"
    RESUBMITTING = "resubmitting"
    POLLING = "polling"
    FETCHING = "fetching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_DOCLING_TRANSITIONS = {
    DoclingPhase.READY: {DoclingPhase.SUBMITTING},
    DoclingPhase.SUBMITTING: {
        DoclingPhase.UNKNOWN_SUBMISSION, DoclingPhase.POLLING, DoclingPhase.FAILED,
        DoclingPhase.TIMED_OUT, DoclingPhase.CANCELLED,
    },
    DoclingPhase.POLLING: {
        DoclingPhase.FETCHING, DoclingPhase.FAILED, DoclingPhase.TIMED_OUT, DoclingPhase.CANCELLED,
    },
    DoclingPhase.FETCHING: {
        DoclingPhase.SUCCEEDED, DoclingPhase.FAILED, DoclingPhase.TIMED_OUT, DoclingPhase.CANCELLED,
    },
    DoclingPhase.UNKNOWN_SUBMISSION: {
        DoclingPhase.RESUBMITTING, DoclingPhase.TIMED_OUT, DoclingPhase.CANCELLED,
    },
    DoclingPhase.RESUBMITTING: {
        DoclingPhase.UNKNOWN_SUBMISSION, DoclingPhase.POLLING, DoclingPhase.FAILED,
        DoclingPhase.TIMED_OUT, DoclingPhase.CANCELLED,
    },
    DoclingPhase.SUCCEEDED: set(),
    DoclingPhase.FAILED: set(),
    DoclingPhase.TIMED_OUT: set(),
    DoclingPhase.CANCELLED: set(),
}


class DoclingSubmissionUnknown(RuntimeError):
    """The server may have accepted work, but supplied no usable task ID."""


class RemoteTaskState(Enum):
    PENDING = "pending"
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: Any) -> "RemoteTaskState":
        try:
            return cls(value)
        except (ValueError, TypeError):
            return cls.UNKNOWN


def _is_retryable_docling_serve_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                            httpx.ReadTimeout, httpx.RemoteProtocolError))


async def _request_once(client: Any, method: str, url: str, deadline: float | None, **kwargs: Any):
    remaining = deadline - time.monotonic() if deadline is not None else 120.0
    if remaining <= 0:
        raise TimeoutError("Docling conversion deadline reached")
    # Bound the complete request as well as individual socket operations.
    async with asyncio.timeout(remaining):
        return await client.request(
            method, url,
            timeout=httpx.Timeout(min(120.0, remaining), connect=min(30.0, remaining)),
            **kwargs,
        )


async def _retry_sleep(delay: float, deadline: float | None) -> None:
    if deadline is None:
        await asyncio.sleep(max(0, delay))
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Docling conversion deadline reached")
    await asyncio.sleep(min(max(0, delay), remaining))


async def _docling_serve_request(
    client: Any, method: str, url: str, *,
    attempts: int = DOCLING_SERVE_REQUEST_ATTEMPTS,
    retry_delay: float = DOCLING_SERVE_RETRY_DELAY_SECONDS,
    deadline: float | None = None,
    conversion: "DoclingConversion | None" = None,
    **kwargs: Any,
):
    if method.upper() == "POST":
        if conversion is None:
            conversion = DoclingConversion(max_submission_attempts=max(1, attempts))
            conversion.transition(DoclingPhase.SUBMITTING)
        return await conversion.submit(client, url, deadline, retry_delay, **kwargs)

    for attempt in range(max(1, attempts)):
        try:
            response = await _request_once(client, method, url, deadline, **kwargs)
        except Exception as exc:
            if attempt + 1 >= max(1, attempts) or not _is_retryable_docling_serve_error(exc):
                raise
        else:
            if response.status_code not in DOCLING_SERVE_RETRY_STATUS_CODES or attempt + 1 >= max(1, attempts):
                return response
        await _retry_sleep(retry_delay, deadline)
    raise RuntimeError("Docling request retries exhausted")


@dataclass
class DoclingConversion:
    phase: DoclingPhase = DoclingPhase.READY
    task_id: str | None = None
    max_submission_attempts: int = DOCLING_SERVE_REQUEST_ATTEMPTS
    submission_attempts: int = 0

    def transition(self, target: DoclingPhase) -> None:
        if target not in _DOCLING_TRANSITIONS[self.phase]:
            raise ValueError(f"Illegal Docling transition: {self.phase.value} -> {target.value}")
        if target in {DoclingPhase.SUBMITTING, DoclingPhase.RESUBMITTING}:
            if self.submission_attempts >= self.max_submission_attempts:
                raise ValueError("Docling submission attempt limit reached")
            self.submission_attempts += 1
        self.phase = target

    async def submit(
        self, client: Any, url: str, deadline: float | None, retry_delay: float, **submission: Any,
    ):
        while True:
            try:
                response = await _request_once(client, "POST", url, deadline, **submission)
                response.raise_for_status()
                try:
                    task_id = response.json()["task_id"]
                    if not isinstance(task_id, str) or not task_id.strip():
                        raise ValueError("missing task ID")
                except (ValueError, KeyError, TypeError) as exc:
                    raise DoclingSubmissionUnknown("Docling returned no usable task ID") from exc
            except Exception as exc:
                retryable = (
                    _is_retryable_docling_serve_error(exc)
                    or isinstance(exc, (DoclingSubmissionUnknown, httpx.WriteError, httpx.WriteTimeout))
                    or (isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code in DOCLING_SERVE_RETRY_STATUS_CODES)
                )
                if not retryable:
                    raise
                self.transition(DoclingPhase.UNKNOWN_SUBMISSION)
                if self.submission_attempts >= self.max_submission_attempts:
                    raise DoclingSubmissionUnknown(
                        f"Docling submission outcome unknown after {self.submission_attempts} attempts; "
                        "no usable task ID was received."
                    ) from exc
                await _retry_sleep(retry_delay, deadline)
                self.transition(DoclingPhase.RESUBMITTING)
                continue
            self.task_id = task_id
            self.transition(DoclingPhase.POLLING)
            return response

    async def run(self, client: Any, base_url: str, timeout: float, **submission: Any) -> dict:
        # Claim the instance before the first await, preventing concurrent reuse.
        self.transition(DoclingPhase.SUBMITTING)
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                await _docling_serve_request(
                    client, "POST", f"{base_url}/v1/convert/file/async",
                    deadline=deadline, conversion=self, **submission,
                )
                task_id = self.task_id
                headers = submission.get("headers", {})
                while True:
                    await asyncio.sleep(min(2.0, max(0, deadline - time.monotonic())))
                    poll = await _docling_serve_request(
                        client, "GET", f"{base_url}/v1/status/poll/{task_id}",
                        headers=headers, deadline=deadline,
                    )
                    poll.raise_for_status()
                    data = poll.json()
                    status = RemoteTaskState.parse(data.get("task_status"))
                    if status is RemoteTaskState.SUCCESS:
                        break
                    if status is RemoteTaskState.FAILURE:
                        raise RuntimeError(f"Docling task {task_id} failed: {data.get('error_message', 'unknown')}")
                    if status is RemoteTaskState.UNKNOWN:
                        raise RuntimeError(f"Docling task {task_id} returned an unknown status: {data.get('task_status')!r}")
                self.transition(DoclingPhase.FETCHING)
                result = await _docling_serve_request(
                    client, "GET", f"{base_url}/v1/result/{task_id}",
                    headers=headers, deadline=deadline,
                )
                result.raise_for_status()
                payload = result.json()
                if not isinstance(payload["document"]["json_content"], dict):
                    raise ValueError("Docling result contains no document object")
                self.transition(DoclingPhase.SUCCEEDED)
                return payload
        except DoclingSubmissionUnknown:
            # submit() leaves this phase terminal only after exhausting its budget.
            raise
        except TimeoutError as exc:
            self.transition(DoclingPhase.TIMED_OUT)
            raise TimeoutError(f"Docling conversion (task {self.task_id}) exceeded its {timeout}s deadline") from exc
        except asyncio.CancelledError:
            self.transition(DoclingPhase.CANCELLED)
            raise
        except Exception as exc:
            self.transition(DoclingPhase.FAILED)
            raise RuntimeError(f"Docling {self.phase.value} (task {self.task_id}): {exc}") from exc


class LocalDoclingPhase(Enum):
    READY = "ready"
    ORIGINAL = "original"
    REPAIRING = "repairing"
    REPAIRED = "repaired"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_LOCAL_DOCLING_TRANSITIONS = {
    LocalDoclingPhase.READY: {LocalDoclingPhase.ORIGINAL},
    LocalDoclingPhase.ORIGINAL: {LocalDoclingPhase.REPAIRING, LocalDoclingPhase.SUCCEEDED},
    LocalDoclingPhase.REPAIRING: {LocalDoclingPhase.REPAIRED, LocalDoclingPhase.FAILED},
    LocalDoclingPhase.REPAIRED: {LocalDoclingPhase.SUCCEEDED, LocalDoclingPhase.FAILED},
    LocalDoclingPhase.SUCCEEDED: set(),
    LocalDoclingPhase.FAILED: set(),
}


@dataclass
class LocalDoclingConversion:
    phase: LocalDoclingPhase = LocalDoclingPhase.READY

    def transition(self, target: LocalDoclingPhase) -> None:
        if target not in _LOCAL_DOCLING_TRANSITIONS[self.phase]:
            raise ValueError(f"Illegal local Docling transition: {self.phase.value} -> {target.value}")
        self.phase = target

    def run(
        self, convert: Callable[[Path], Any], repair: Callable[[Path, Path], bool],
        source: Path, repaired: Path,
    ) -> tuple[Any, Path]:
        self.transition(LocalDoclingPhase.ORIGINAL)
        try:
            result = convert(source)
        except Exception:
            self.transition(LocalDoclingPhase.REPAIRING)
            try:
                repair_succeeded = repair(source, repaired)
            except Exception:
                self.transition(LocalDoclingPhase.FAILED)
                raise
            if not repair_succeeded:
                self.transition(LocalDoclingPhase.FAILED)
                raise
            self.transition(LocalDoclingPhase.REPAIRED)
            try:
                result = convert(repaired)
            except Exception:
                self.transition(LocalDoclingPhase.FAILED)
                raise
            self.transition(LocalDoclingPhase.SUCCEEDED)
            return result, repaired
        self.transition(LocalDoclingPhase.SUCCEEDED)
        return result, source
