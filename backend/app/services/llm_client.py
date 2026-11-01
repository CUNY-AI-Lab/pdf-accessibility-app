"""Runner model requests through the job's Worker relay, with bounded retries.

Transitions::
    waiting -> requesting | failed | cancelled
    requesting -> backoff | succeeded | failed | cancelled
    backoff -> requesting | failed | cancelled
    succeeded/failed/cancelled -> terminal
    access: available -> refused; refused -> refused (retain first reason)

Each request has at most max_retries + 1 attempts and capped backoff. Connection
failures and selected HTTP statuses retry; uncertain read timeouts do not.
Access is checked before dispatch, after semaphore acquisition and on response.
A new job scope owns a new access latch; nested scopes share the outer latch.
"""

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from app.config import DEFAULT_LLM_MAX_REQUEST_BYTES
from app.services.vision_requests import bound_vision_request

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
_ACTIVE_USAGE_RECORDER: ContextVar["LlmUsageRecorder | None"] = ContextVar(
    "active_llm_usage_recorder",
    default=None,
)


@dataclass
class LlmUsageRecorder:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def record(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.request_count += 1
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.total_tokens += max(0, int(total_tokens))
        self.cost_usd += max(0.0, float(cost_usd))


@contextmanager
def track_llm_usage():
    recorder = LlmUsageRecorder()
    token = _ACTIVE_USAGE_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE_USAGE_RECORDER.reset(token)


@dataclass(frozen=True)
class ModelEndpoint:
    """Where a job's model calls go and the headers that authorize them."""

    base_url: str
    headers: dict[str, str] = field(default_factory=dict)


def settings_model_endpoint(settings: Any) -> ModelEndpoint:
    token = str(settings.llm_api_key or "").strip()
    return ModelEndpoint(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


class ModelAccessRefused(Exception):
    """The Gateway refused the job's credential, so no later call can succeed.

    401 means the credential was not accepted, 403 that the person's access no
    longer covers model use, and a 429 marked ``x-should-retry: false`` that the
    person's budget or the Lab's ceiling is used up.
    """

    def __init__(self, status_code: int, code: str, provider_message: str):
        self.status_code = status_code
        self.code = code
        self.provider_message = provider_message
        super().__init__(self.user_message)

    @property
    def user_message(self) -> str:
        if self.status_code == 401:
            return (
                "The CAIL Gateway did not accept this job's sign-in. "
                "Sign in again and upload the PDF again."
            )
        if self.status_code == 403:
            return (
                "Your CUNY AI Lab access no longer covers model use, so this PDF "
                "could not be finished."
            )
        return (
            "Your CUNY AI Lab model budget is used up for now, so this PDF could "
            "not be finished. Upload it again after your budget resets."
        )


class ModelAccessState(Enum):
    AVAILABLE = "available"
    REFUSED = "refused"


class ModelAccess:
    """A refusal is terminal within one job; a new scope creates new access."""

    def __init__(self, endpoint: ModelEndpoint | None = None):
        self.endpoint = endpoint
        self._refusal: ModelAccessRefused | None = None

    @property
    def state(self) -> ModelAccessState:
        return ModelAccessState.REFUSED if self._refusal else ModelAccessState.AVAILABLE

    @property
    def refusal(self) -> ModelAccessRefused | None:
        return self._refusal

    @refusal.setter
    def refusal(self, reason: ModelAccessRefused) -> None:
        self.refuse(reason)

    def refuse(self, reason: ModelAccessRefused) -> ModelAccessRefused:
        if not isinstance(reason, ModelAccessRefused):
            raise ValueError("Illegal model access transition: refusal requires a reason")
        # Multiple in-flight requests may be refused; retain the first reason.
        if self.state is ModelAccessState.AVAILABLE:
            self._refusal = reason
        return self._refusal


class RequestState(Enum):
    WAITING = "waiting"
    REQUESTING = "requesting"
    BACKOFF = "backoff"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_REQUEST_TRANSITIONS = {
    RequestState.WAITING: {RequestState.REQUESTING, RequestState.FAILED, RequestState.CANCELLED},
    RequestState.REQUESTING: {
        RequestState.BACKOFF, RequestState.SUCCEEDED, RequestState.FAILED, RequestState.CANCELLED,
    },
    RequestState.BACKOFF: {RequestState.REQUESTING, RequestState.FAILED, RequestState.CANCELLED},
    RequestState.SUCCEEDED: set(),
    RequestState.FAILED: set(),
    RequestState.CANCELLED: set(),
}


@dataclass
class RequestAttempt:
    max_attempts: int
    state: RequestState = RequestState.WAITING
    attempts: int = 0

    def transition(self, state: RequestState) -> None:
        if state not in _REQUEST_TRANSITIONS[self.state]:
            raise ValueError(f"Illegal LLM request transition: {self.state.value} -> {state.value}")
        if state is RequestState.REQUESTING:
            if self.attempts >= self.max_attempts:
                raise ValueError("LLM request attempt limit reached")
            self.attempts += 1
        self.state = state


def refuse_model_access(reason: ModelAccessRefused) -> ModelAccessRefused:
    access = _MODEL_ACCESS.get()
    return access.refuse(reason) if access is not None else reason


_MODEL_ACCESS: ContextVar[ModelAccess | None] = ContextVar("job_model_access", default=None)


@contextmanager
def job_model_access(endpoint: ModelEndpoint | None = None):
    """Scope model calls to one job; nested scopes share the outer one.

    Child tasks and threads copy the context, so a refusal seen in any of them
    is seen by all.
    """
    current = _MODEL_ACCESS.get()
    if current is not None and endpoint is None:
        yield current
        return
    access = ModelAccess(endpoint=endpoint)
    token = _MODEL_ACCESS.set(access)
    try:
        yield access
    finally:
        _MODEL_ACCESS.reset(token)


def current_model_endpoint(settings: Any) -> ModelEndpoint:
    access = _MODEL_ACCESS.get()
    if access is not None and access.endpoint is not None:
        return access.endpoint
    return settings_model_endpoint(settings)


def raise_if_model_access_refused() -> None:
    access = _MODEL_ACCESS.get()
    if access is not None and access.refusal is not None:
        raise access.refusal


def _refusal_from(response: httpx.Response) -> ModelAccessRefused | None:
    status = response.status_code
    if status == 429 and response.headers.get("x-should-retry", "").lower() != "false":
        return None
    if status not in {401, 403, 429}:
        return None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
    except ValueError:
        error = None
    code = str(error.get("code") or error.get("type") or "") if isinstance(error, dict) else ""
    return ModelAccessRefused(status, code or str(status), _error_message(response))


def _is_retryable(exc: BaseException) -> bool:
    # Repeat only a request that never reached the provider. After a read
    # timeout the model may have run, and been paid for, anyway.
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


def is_retryable_llm_exception(exc: BaseException) -> bool:
    """Return True when an LLM failure is transient and retry-oriented."""
    return _is_retryable(exc)


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    retry_after = (exc.response.headers.get("Retry-After") or "").strip()
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
    except ValueError:
        try:
            retry_after_dt = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_after_dt.tzinfo is None:
            retry_after_dt = retry_after_dt.replace(tzinfo=UTC)
        seconds = (retry_after_dt - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)


def _retry_delay_seconds(
    exc: BaseException,
    *,
    attempt_number: int,
    retry_backoff_base: float,
    max_backoff_seconds: float,
) -> float:
    retry_after_seconds = _retry_after_seconds(exc)
    if retry_after_seconds is not None:
        return min(retry_after_seconds, max_backoff_seconds)
    delay = retry_backoff_base ** max(0, attempt_number - 1)
    return min(delay, max_backoff_seconds)


def _usage_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _record_response_usage(response_json: dict) -> None:
    recorder = _ACTIVE_USAGE_RECORDER.get()
    if recorder is None or not isinstance(response_json, dict):
        return
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return
    prompt_tokens = _usage_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0))
    )
    completion_tokens = _usage_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    total_tokens = _usage_int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    cost_usd = _usage_float(usage.get("cost", usage.get("total_cost", 0.0)))
    recorder.record(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


class LlmClient:
    """Thin wrapper around a chat-completions API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        max_concurrency: int = 2,
        headers: dict[str, str] | None = None,
        max_request_bytes: int = DEFAULT_LLM_MAX_REQUEST_BYTES,
    ):
        if headers is None:
            token = api_key.strip()
            headers = {"Authorization": f"Bearer {token}"} if token else {}

        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base = max(1.0, float(retry_backoff_base))
        self.max_backoff_seconds = max(1.0, float(max_backoff_seconds))
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=self.max_concurrency,
                max_keepalive_connections=self.max_concurrency,
            ),
        )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_request_bytes = max_request_bytes

    async def prepare_request(self, messages: list[dict], **kwargs) -> dict:
        return await asyncio.to_thread(
            bound_vision_request,
            {"model": self.model, "messages": messages, **kwargs},
            self.max_request_bytes,
        )

    async def chat_completion(self, messages: list[dict], **kwargs) -> dict:
        attempt = RequestAttempt(self.max_retries + 1)
        try:
            raise_if_model_access_refused()
            prepared = await self.prepare_request(messages, **kwargs)
            messages = prepared.pop("messages")
            prepared.pop("model")
            while True:
                raise_if_model_access_refused()
                attempt.transition(RequestState.REQUESTING)
                try:
                    response = await self._do_chat_completion(messages, **prepared)
                except Exception as exc:
                    if not _is_retryable(exc) or attempt.attempts >= attempt.max_attempts:
                        raise
                    attempt.transition(RequestState.BACKOFF)
                    delay = _retry_delay_seconds(
                        exc,
                        attempt_number=attempt.attempts,
                        retry_backoff_base=self.retry_backoff_base,
                        max_backoff_seconds=self.max_backoff_seconds,
                    )
                    logger.warning("LLM request failed (%s), retrying in %.1fs [%s/%s]",
                                   type(exc).__name__, delay, attempt.attempts, attempt.max_attempts)
                    await asyncio.sleep(delay)
                    continue
                attempt.transition(RequestState.SUCCEEDED)
                return response
        except asyncio.CancelledError:
            attempt.transition(RequestState.CANCELLED)
            raise
        except Exception:
            attempt.transition(RequestState.FAILED)
            raise

    async def _do_chat_completion(self, messages: list[dict], **kwargs) -> dict:
        raise_if_model_access_refused()
        async with self._semaphore:
            raise_if_model_access_refused()
            response = await self.client.post(
                "/chat/completions",
                json={"model": self.model, "messages": messages, **kwargs},
            )
            if response.is_error:
                logger.warning(
                    "LLM request to %s refused (%s): %s",
                    self.model,
                    response.status_code,
                    _error_message(response),
                )
            refusal = _refusal_from(response)
            if refusal is not None:
                raise refuse_model_access(refusal)
            raise_if_model_access_refused()
            response.raise_for_status()
            payload = response.json()
            _record_response_usage(payload)
            return payload

    async def close(self):
        await self.client.aclose()


def _error_message(response: httpx.Response) -> str:
    """The provider's own explanation of a refused request, if it gave one."""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
    except ValueError:
        return response.text[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or response.text)[:300]


def make_llm_client(settings: Any) -> LlmClient:
    return make_llm_client_with_overrides(settings)


def make_llm_client_with_overrides(
    settings: Any,
    *,
    timeout: int | None = None,
    max_retries: int | None = None,
    retry_backoff_base: float | None = None,
    max_backoff_seconds: float | None = None,
    max_concurrency: int | None = None,
) -> LlmClient:
    endpoint = current_model_endpoint(settings)
    return LlmClient(
        base_url=endpoint.base_url,
        api_key="",
        headers=endpoint.headers,
        model=settings.llm_model,
        max_request_bytes=getattr(settings, "llm_max_request_bytes", DEFAULT_LLM_MAX_REQUEST_BYTES),
        timeout=settings.llm_timeout if timeout is None else timeout,
        max_retries=settings.llm_max_retries if max_retries is None else max_retries,
        retry_backoff_base=(
            settings.llm_retry_backoff_base if retry_backoff_base is None else retry_backoff_base
        ),
        max_backoff_seconds=(
            settings.llm_retry_max_backoff_seconds
            if max_backoff_seconds is None
            else max_backoff_seconds
        ),
        max_concurrency=settings.llm_max_concurrency if max_concurrency is None else max_concurrency,
    )
