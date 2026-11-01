import asyncio
import io
import json
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models import AltTextEntry, AppliedChange, Job, ReviewTask
from app.runner import tasks as runner_tasks
from app.runner import views as runner_views
from app.runner.client import JOB_TOKEN_HEADER, RUNNER_TOKEN_HEADER, Task, UploadPhase, WorkerClient
from app.runner.state import (
    database_path,
    input_path,
    open_job_database,
    pack_state,
    remove_job_files,
    unpack_state,
)
from app.runner.views import preview_pages_for_task
from app.services.llm_client import current_model_endpoint
from tests.pdf_fixtures import tagged_text_pages

WORKER = "https://tools.test/pdf-accessibility/runner"
JOB_ID = "0b3c9a52-6f0e-4b8e-9d1c-1f6a3c2b7e10"
PDF = b"%PDF-1.7\n%test\n"


def _settings(tmp_path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        upload_dir=data / "uploads",
        processing_dir=data / "processing",
        output_dir=data / "output",
        runner_worker_url=WORKER,
        runner_token="host-token",
        llm_api_key="",
        runner_heartbeat_seconds=0.05,
    )


def _task(kind="process", **overrides) -> Task:
    data = {
        "job_id": JOB_ID,
        "kind": kind,
        "lease": "lease-token",
        "attempt": 1,
        "deadline": int((time.time() + 600) * 1000),
        "original_filename": "syllabus.pdf",
        **overrides,
    }
    return Task.from_json(data)


class FakeWorker:
    """The Worker's runner API, recorded."""

    def __init__(
        self,
        *,
        state: bytes | None = None,
        progress_status: int = 200,
        refuse_once: asyncio.Event | None = None,
    ):
        self.state = state
        self.progress_status = progress_status
        # When given, progress answers 200 until the event is set.
        self.refuse_once = refuse_once
        self.uploads: dict[str, tuple[str, bytes]] = {}
        self.progress: list[dict] = []
        self.completed: dict | None = None
        self.failed: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers[RUNNER_TOKEN_HEADER] == "host-token"
        assert request.headers[JOB_TOKEN_HEADER] == "lease-token"
        path = request.url.path.removeprefix("/pdf-accessibility/runner")
        prefix = f"/tasks/{JOB_ID}"
        assert path.startswith(prefix)
        action = path.removeprefix(prefix)
        if action == "/input":
            return httpx.Response(200, content=PDF)
        if action == "/state":
            return httpx.Response(200, content=self.state)
        if action == "/progress":
            self.progress.append(json.loads(request.content))
            if self.refuse_once is not None and not self.refuse_once.is_set():
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(self.progress_status, json={"ok": True})
        if action.startswith("/files/"):
            self.uploads[action.removeprefix("/files/")] = (
                request.headers["content-type"],
                request.content,
            )
            return httpx.Response(204)
        if action == "/complete":
            self.completed = json.loads(request.content)
            return httpx.Response(204)
        if action == "/fail":
            self.failed = json.loads(request.content)["error"]
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def client(self) -> WorkerClient:
        return WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(self.handler))


async def _finish_as_reviewed(session_maker, settings, job_id: str) -> None:
    """What a finished pipeline leaves: output, a figure, a change, and a task."""
    figure = settings.processing_dir / job_id / "figures" / "figure_0.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_bytes(b"figure-png")
    output = settings.output_dir / job_id / "accessible_syllabus.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(PDF)
    async with session_maker() as db:
        job = await db.get(Job, job_id)
        job.status = "manual_remediation"
        job.page_count = 4
        job.output_path = str(output)
        job.validation_json = json.dumps({"compliant": False, "remediation": {}})
        db.add(
            AltTextEntry(
                job_id=job_id,
                figure_index=0,
                image_path=str(figure),
                generated_text="A bar chart",
                status="approved",
            )
        )
        await db.flush()
        db.add(
            AppliedChange(
                job_id=job_id,
                change_type="figure_semantics",
                title="Described figure 1",
                detail="Generated alt text.",
                review_status="pending_review",
                reviewable=True,
                metadata_json=json.dumps({"figure_index": 0, "page": 2}),
                undo_payload_json=json.dumps(
                    {"kind": "alt_text_entry", "figure_index": 0, "status": "pending_review"}
                ),
            )
        )
        db.add(
            ReviewTask(
                job_id=job_id,
                task_type="alt_text",
                title="Check figure descriptions",
                detail="Confirm the generated alt text.",
                blocking=False,
                metadata_json=json.dumps({"pages_to_check": [2, 9]}),
            )
        )
        await db.commit()


@pytest.fixture
def quiet_previews(monkeypatch):
    rendered: list[int] = []

    def render(path, page, **_kwargs):
        rendered.append(page)
        return b"preview-png"

    monkeypatch.setattr(runner_views, "render_page_png_bytes", render)
    monkeypatch.setattr(runner_tasks, "preflight_pdf_upload", lambda path, settings: None)
    return rendered


@pytest.mark.asyncio
async def test_process_task_runs_through_the_relay_and_uploads_views(
    tmp_path, monkeypatch, quiet_previews
):
    settings = _settings(tmp_path)
    worker = FakeWorker()
    client = worker.client()
    seen = {}

    async def fake_pipeline(job_id, session_maker, settings_arg, sink):
        seen["endpoint"] = current_model_endpoint(settings_arg)
        sink.emit_progress(job_id, step="classify", status="running")
        await asyncio.sleep(0.12)
        await _finish_as_reviewed(session_maker, settings_arg, job_id)

    monkeypatch.setattr(runner_tasks, "run_pipeline", fake_pipeline)
    await runner_tasks.run_task(client, _task(), settings)
    await client.close()

    assert seen["endpoint"].base_url == f"{WORKER}/tasks/{JOB_ID}/v1"
    assert seen["endpoint"].headers == {
        RUNNER_TOKEN_HEADER: "host-token",
        JOB_TOKEN_HEADER: "lease-token",
    }
    assert {"step": "classify", "status": "running"} in worker.progress
    assert {} in worker.progress
    assert worker.failed is None
    assert set(worker.uploads) == {
        "views/review-tasks.json",
        "views/applied-changes.json",
        "views/figure-changes.json",
        "views/report.json",
        "views/batch-parts.json",
        "report.html",
        "output.pdf",
        "figures/0",
        "previews/2",
        "state.tar.gz",
    }
    assert quiet_previews == [2]
    assert worker.uploads["output.pdf"] == ("application/pdf", PDF)
    assert worker.completed["job"]["status"] == "manual_remediation"
    assert worker.completed["figures"] == [0]
    assert worker.completed["previews"] == [2]
    assert worker.completed["has_state"] is True
    assert not input_path(settings, JOB_ID).exists()
    assert not database_path(settings, JOB_ID).exists()


@pytest.mark.asyncio
async def test_preflight_refusal_fails_the_task_with_its_reason(tmp_path, monkeypatch):
    from app.services.pdf_preflight import PdfUploadPreflightError

    settings = _settings(tmp_path)
    worker = FakeWorker()
    client = worker.client()

    def refuse(path, settings):
        raise PdfUploadPreflightError("This PDF has more than 300 pages.")

    monkeypatch.setattr(runner_tasks, "preflight_pdf_upload", refuse)
    await runner_tasks.run_task(client, _task(), settings)
    await client.close()

    assert worker.failed == "This PDF has more than 300 pages."
    assert worker.completed is None


@pytest.mark.parametrize("status", ["complete", "manual_remediation"])
async def test_process_uploads_accessible_html_from_final_pdf(
    tmp_path, monkeypatch, quiet_previews, status
):
    settings = _settings(tmp_path)
    worker = FakeWorker()
    client = worker.client()
    final_pdf = b""

    async def fake_pipeline(job_id, session_maker, settings_arg, sink):
        nonlocal final_pdf
        await _finish_as_reviewed(session_maker, settings_arg, job_id)
        async with session_maker() as db:
            job = await db.get(Job, job_id)
            tagged_text_pages(
                settings_arg.output_dir / job_id / "accessible_syllabus.pdf",
                actual_text="Final reviewed text & fixes",
                running_head=True,
            )
            final_pdf = (settings_arg.output_dir / job_id / "accessible_syllabus.pdf").read_bytes()
            job.status = status
            await db.commit()

    monkeypatch.setattr(runner_tasks, "run_pipeline", fake_pipeline)
    await runner_tasks.run_task(client, _task(), settings)
    await client.close()

    assert worker.failed is None
    assert worker.completed["job"]["status"] == status
    assert "document.html" in worker.completed["files"]
    content_type, document = worker.uploads["document.html"]
    assert content_type == "text/html; charset=utf-8"
    assert b"Final reviewed text &amp; fixes" in document
    assert b"running head" not in document
    assert document.startswith(b"<!doctype html>")
    assert worker.uploads["output.pdf"] == ("application/pdf", final_pdf)


@pytest.mark.parametrize("status", ["complete", "manual_remediation"])
async def test_html_export_failure_still_completes_pdf_with_safe_diagnostic(
    tmp_path,
    monkeypatch,
    quiet_previews,
    caplog,
    status,
):
    settings = _settings(tmp_path)
    worker = FakeWorker()
    client = worker.client()

    async def fake_pipeline(job_id, session_maker, settings_arg, sink):
        await _finish_as_reviewed(session_maker, settings_arg, job_id)
        async with session_maker() as db:
            job = await db.get(Job, job_id)
            job.status = status
            await db.commit()

    def failed_export(path):
        assert path.name == "accessible_syllabus.pdf"
        raise RuntimeError("DOCUMENT_SECRET /private/sensitive.pdf")

    monkeypatch.setattr(runner_tasks, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(runner_views, "export_html", failed_export)
    await runner_tasks.run_task(client, _task(), settings)
    await client.close()

    assert worker.failed is None
    assert worker.completed["job"]["status"] == status
    assert worker.uploads["output.pdf"] == ("application/pdf", PDF)
    assert "document.html" not in worker.uploads
    assert "document.html" not in worker.completed["files"]
    records = [record for record in caplog.records if record.name == runner_views.__name__]
    assert len(records) == 1
    assert json.loads(records[0].getMessage()) == {
        "event": "html_export_failed",
        "job_id": JOB_ID,
        "error_type": "RuntimeError",
    }
    assert records[0].exc_info is None
    assert "DOCUMENT_SECRET" not in caplog.text
    assert "/private/sensitive.pdf" not in caplog.text


async def _saved_state(settings, *, actual_text=None) -> bytes:
    engine, session_maker = await open_job_database(settings, JOB_ID)
    pdf = input_path(settings, JOB_ID)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(PDF)
    async with session_maker() as db:
        db.add(
            Job(
                id=JOB_ID,
                filename=pdf.name,
                original_filename="syllabus.pdf",
                owner_session_hash="runner",
                input_path=str(pdf),
            )
        )
        await db.commit()
    await _finish_as_reviewed(session_maker, settings, JOB_ID)
    if actual_text is not None:
        tagged_text_pages(
            settings.output_dir / JOB_ID / "accessible_syllabus.pdf",
            actual_text=actual_text,
        )
    await engine.dispose()
    archive = pack_state(settings, JOB_ID, settings.upload_dir.parent / "saved.tar.gz")
    body = archive.read_bytes()
    archive.unlink()
    remove_job_files(settings, JOB_ID)
    return body


@pytest.mark.parametrize("kind", ["review", "sync"])
async def test_later_tasks_regenerate_accessible_html(tmp_path, monkeypatch, quiet_previews, kind):
    settings = _settings(tmp_path)
    worker = FakeWorker(
        state=await _saved_state(settings, actual_text="Previously reviewed content")
    )
    client = worker.client()

    async def fake_rerun(job_id, db, settings_arg, sink):
        tagged_text_pages(
            settings_arg.output_dir / job_id / "accessible_syllabus.pdf",
            actual_text="New reviewer fix & reading",
        )
        job = await db.get(Job, job_id)
        job.status = "manual_remediation"
        job.output_path = str(settings_arg.output_dir / job_id / "accessible_syllabus.pdf")
        await db.commit()

    monkeypatch.setattr(runner_tasks, "run_tagging_and_validation", fake_rerun)
    task = _task(kind, action={"type": "edit", "change_id": 1, "text": "Revised description"})
    await runner_tasks.run_task(client, task, settings)
    await client.close()

    assert worker.failed is None
    assert "document.html" in worker.completed["files"]
    document = worker.uploads["document.html"][1]
    if kind == "review":
        assert b"New reviewer fix &amp; reading" in document
        assert b"Previously reviewed content" not in document
    else:
        assert b"Previously reviewed content" in document
    with tarfile.open(
        fileobj=io.BytesIO(worker.uploads["state.tar.gz"][1]), mode="r:gz"
    ) as archive:
        assert not any(member.name.endswith(".html") for member in archive.getmembers())


@pytest.mark.asyncio
async def test_review_edit_restores_state_and_reruns_tagging(tmp_path, monkeypatch, quiet_previews):
    settings = _settings(tmp_path)
    worker = FakeWorker(state=await _saved_state(settings))
    client = worker.client()
    reruns: list[str] = []

    async def fake_rerun(job_id, db, settings_arg, sink):
        reruns.append(job_id)
        job = await db.get(Job, job_id)
        assert job.status == "processing"
        job.status = "manual_remediation"
        job.output_path = str(settings_arg.output_dir / job_id / "accessible_syllabus.pdf")
        job.validation_json = json.dumps({"compliant": False, "remediation": {}})
        await db.commit()

    monkeypatch.setattr(runner_tasks, "run_tagging_and_validation", fake_rerun)
    task = _task(
        "review",
        action={"type": "edit", "change_id": 1, "text": "Enrollment by year, 2019 to 2024"},
        overrides={"tasks": {"1": "resolved"}},
        overrides_version=3,
    )
    await runner_tasks.run_task(client, task, settings)
    await client.close()

    assert reruns == [JOB_ID]
    assert worker.failed is None
    changes = json.loads(worker.uploads["views/figure-changes.json"][1])
    assert changes[-1]["after"]["edited_text"] == "Enrollment by year, 2019 to 2024"
    tasks = json.loads(worker.uploads["views/review-tasks.json"][1])
    assert tasks[0]["status"] == "resolved"
    assert worker.completed["overrides_version"] == 3
    assert worker.completed["job"]["status"] == "manual_remediation"


@pytest.mark.asyncio
async def test_refused_review_action_keeps_the_previous_result(
    tmp_path, monkeypatch, quiet_previews
):
    settings = _settings(tmp_path)
    worker = FakeWorker(state=await _saved_state(settings))
    client = worker.client()

    async def fake_rerun(*_args):
        raise AssertionError("a refused action must not re-run tagging")

    monkeypatch.setattr(runner_tasks, "run_tagging_and_validation", fake_rerun)
    task = _task(
        "review", action={"type": "undo", "change_id": 1}, overrides={"changes": {"1": "kept"}}
    )
    await runner_tasks.run_task(client, task, settings)
    await client.close()

    assert worker.uploads == {}
    assert worker.completed == {
        "review_refused": (
            "This figure decision is no longer pending review. Refresh to see the latest job state."
        ),
        "overrides_version": 0,
    }


@pytest.mark.asyncio
async def test_lost_lease_stops_the_pipeline(tmp_path, monkeypatch, quiet_previews):
    settings = _settings(tmp_path)
    started = asyncio.Event()
    # The lease is lost only once the pipeline is running, as it would be.
    worker = FakeWorker(progress_status=409, refuse_once=started)
    client = worker.client()
    cancelled = asyncio.Event()

    async def slow_pipeline(job_id, session_maker, settings_arg, sink):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(runner_tasks, "run_pipeline", slow_pipeline)
    await asyncio.wait_for(runner_tasks.run_task(client, _task(), settings), timeout=5)
    await client.close()

    assert cancelled.is_set()
    assert worker.completed is None
    assert worker.failed is None


@pytest.mark.asyncio
async def test_deadline_stops_the_pipeline_and_fails_the_job(tmp_path, monkeypatch, quiet_previews):
    settings = _settings(tmp_path)
    settings.runner_heartbeat_seconds = 30
    worker = FakeWorker()
    client = worker.client()

    async def slow_pipeline(*_args):
        await asyncio.sleep(30)

    monkeypatch.setattr(runner_tasks, "run_pipeline", slow_pipeline)
    task = _task(deadline=int((time.time() + 0.2) * 1000))
    await asyncio.wait_for(runner_tasks.run_task(client, task, settings), timeout=5)
    await client.close()

    assert worker.failed == runner_tasks.DEADLINE_ERROR


def test_unpack_refuses_paths_outside_the_job(tmp_path):
    settings = _settings(tmp_path)
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("processing/another-job/secret.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    settings.upload_dir.parent.mkdir(parents=True)

    with pytest.raises(ValueError, match="unexpected path"):
        unpack_state(settings, JOB_ID, archive)


def test_preview_pages_match_the_review_screen():
    metadata = {
        "pages_to_check": [5, "2", 0, "x"],
        "targets": [{"page": 7}, {"page": True}, "bad"],
        "broken_links": [{"page": 1}],
    }
    assert preview_pages_for_task(metadata) == [1, 2, 5]


def _publication_outcome():
    return runner_tasks.Outcome(
        views=runner_views.RenderedViews(
            job={"status": "failed"},
            files=[runner_views.RenderedFile("report.html", b"report", "text/html")],
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_path", ["/files/report.html", "/complete"])
async def test_lease_is_renewed_until_publication_is_acknowledged(
    tmp_path, monkeypatch, blocked_path
):
    worker = FakeWorker()
    publishing = asyncio.Event()
    renewed = asyncio.Event()

    async def handler(request):
        if request.url.path.endswith(blocked_path):
            publishing.set()
            await asyncio.wait_for(renewed.wait(), 1)
        if request.url.path.endswith("/progress") and publishing.is_set():
            renewed.set()
        return worker.handler(request)

    async def perform(*_args):
        return _publication_outcome()

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    execution = runner_tasks.ClaimedTaskExecution(client, _task(), _settings(tmp_path))
    await asyncio.wait_for(execution.run(), 2)
    await client.close()
    assert renewed.is_set()
    assert worker.completed == {
        "job": {"status": "failed"},
        "figures": [],
        "previews": [],
        "files": ["report.html"],
        "has_state": False,
        "overrides_version": 0,
    }
    assert execution.phase is runner_tasks.TaskPhase.DONE


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_source", ["heartbeat", "upload"])
async def test_lease_loss_during_publication_stops_without_completion(
    tmp_path, monkeypatch, loss_source
):
    worker = FakeWorker()
    uploading = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request):
        if "/files/" in request.url.path:
            uploading.set()
            if loss_source == "upload":
                return httpx.Response(409)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        if request.url.path.endswith("/progress") and uploading.is_set():
            return httpx.Response(409)
        return worker.handler(request)

    async def perform(*_args):
        return _publication_outcome()

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    execution = runner_tasks.ClaimedTaskExecution(client, _task(), _settings(tmp_path))
    await asyncio.wait_for(execution.run(), 2)
    await client.close()
    assert execution.phase is runner_tasks.TaskPhase.FAILED
    assert worker.completed is None
    assert worker.failed is None
    assert worker.uploads == {}
    if loss_source == "heartbeat":
        assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_source", ["work", "publication", "outcome"])
async def test_failure_reporting_keeps_renewing_until_acknowledged(
    tmp_path, monkeypatch, failure_source
):
    worker = FakeWorker()
    reporting = asyncio.Event()
    renewed = asyncio.Event()

    async def handler(request):
        if "/files/" in request.url.path and failure_source == "publication":
            raise RuntimeError("upload failed")
        if request.url.path.endswith("/fail"):
            reporting.set()
            await asyncio.wait_for(renewed.wait(), 1)
        if request.url.path.endswith("/progress") and reporting.is_set():
            renewed.set()
        return worker.handler(request)

    async def perform(*_args):
        if failure_source == "work":
            raise RuntimeError("work failed")
        if failure_source == "outcome":
            return runner_tasks.Outcome(failed="Preflight refused")
        return _publication_outcome()

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    execution = runner_tasks.ClaimedTaskExecution(client, _task(), _settings(tmp_path))
    await asyncio.wait_for(execution.run(), 2)
    await client.close()
    assert renewed.is_set()
    assert worker.failed == (
        "Preflight refused" if failure_source == "outcome" else runner_tasks.INTERNAL_ERROR
    )
    assert worker.completed is None
    assert execution.phase is runner_tasks.TaskPhase.FAILED


@pytest.mark.asyncio
async def test_deadline_applies_during_publication(tmp_path, monkeypatch):
    worker = FakeWorker()
    cancelled = asyncio.Event()

    async def handler(request):
        if "/files/" in request.url.path:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return worker.handler(request)

    async def perform(*_args):
        return _publication_outcome()

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    task = _task(deadline=int((time.time() + 0.15) * 1000))
    await asyncio.wait_for(runner_tasks.run_task(client, task, _settings(tmp_path)), 2)
    await client.close()
    assert cancelled.is_set()
    assert worker.failed == runner_tasks.DEADLINE_ERROR
    assert worker.completed is None


@pytest.mark.asyncio
async def test_unknown_completion_status_is_failed_before_upload(tmp_path, monkeypatch):
    worker = FakeWorker()
    client = worker.client()

    async def perform(*_args):
        outcome = _publication_outcome()
        outcome.views.job["status"] = "processing"
        return outcome

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    await runner_tasks.run_task(client, _task(), _settings(tmp_path))
    await client.close()
    assert worker.failed == runner_tasks.INTERNAL_ERROR
    assert worker.uploads == {}
    assert worker.completed is None


def test_runner_rejects_illegal_lifecycle_transition(tmp_path):
    execution = runner_tasks.ClaimedTaskExecution(None, _task(), _settings(tmp_path))
    with pytest.raises(ValueError, match="claimed -> done"):
        execution.transition(runner_tasks.TaskPhase.DONE)


@pytest.mark.asyncio
async def test_lease_loss_while_packing_prevents_state_upload(tmp_path, monkeypatch):
    worker = FakeWorker()
    packing = asyncio.Event()
    release = threading.Event()
    pack_done = threading.Event()
    loop = asyncio.get_running_loop()

    def pack(*_args, **_kwargs):
        loop.call_soon_threadsafe(packing.set)
        try:
            assert release.wait(2)
        finally:
            pack_done.set()

    async def handler(request):
        if request.url.path.endswith("/progress") and packing.is_set():
            release.set()
            return httpx.Response(409)
        return worker.handler(request)

    async def perform(*_args):
        outcome = _publication_outcome()
        outcome.views.job["status"] = "complete"
        return outcome

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    monkeypatch.setattr(runner_tasks, "pack_state", pack)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    try:
        await asyncio.wait_for(runner_tasks.run_task(client, _task(), _settings(tmp_path)), 1)
    finally:
        release.set()
        assert await asyncio.to_thread(pack_done.wait, 2)
        await client.close()
    assert set(worker.uploads) == {"report.html"}
    assert worker.completed is None
    assert worker.failed is None


@pytest.mark.asyncio
async def test_lease_loss_during_failure_reporting_does_not_report_again(tmp_path, monkeypatch):
    worker = FakeWorker()
    failures = 0

    async def handler(request):
        nonlocal failures
        if request.url.path.endswith("/fail"):
            failures += 1
            return httpx.Response(409)
        return worker.handler(request)

    async def perform(*_args):
        raise RuntimeError("work failed")

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    await runner_tasks.run_task(client, _task(), _settings(tmp_path))
    await client.close()
    assert failures == 1
    assert worker.completed is None
    assert worker.uploads == {}


@pytest.mark.parametrize("current", list(UploadPhase))
def test_upload_guard_all_transitions(current):
    from app.runner.client import UploadPhase, upload_transition

    expected = {
        "sending": {"backoff", "acknowledged", "failed", "lease_lost"},
        "backoff": {"sending", "failed"},
        "acknowledged": set(),
        "failed": set(),
        "lease_lost": set(),
    }
    for target in UploadPhase:
        if target.value in expected[current.value]:
            assert upload_transition(current, target) is target
        else:
            with pytest.raises(ValueError, match="Illegal upload transition"):
                upload_transition(current, target)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 411, 413, 422])
async def test_upload_does_not_retry_permanent_errors(tmp_path, status):
    from app.runner.client import LeaseLost

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status)

    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    with pytest.raises(LeaseLost if status in {404, 409, 410} else httpx.HTTPStatusError):
        await client.upload(_task(), "output.pdf", b"pdf", "application/pdf")
    await client.close()
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [408, 425, 429, 500, 502, 503, 504, "transport"])
async def test_upload_retries_transient_errors_with_identical_bytes(tmp_path, monkeypatch, failure):
    from app.runner import client as runner_client

    requests = []
    sleeps = []
    source = tmp_path / "output.pdf"
    source.write_bytes(b"original")

    async def sleep(seconds):
        sleeps.append(seconds)

    def handler(request):
        requests.append(request.content)
        source.write_bytes(b"changed while retrying")
        if len(requests) == 3:
            return httpx.Response(204)
        if failure == "transport":
            raise httpx.ReadError("uncertain accepted PUT")
        return httpx.Response(failure)

    monkeypatch.setattr(runner_client.asyncio, "sleep", sleep)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    await client.upload(_task(), "output.pdf", source, "application/pdf")
    await client.close()
    assert requests == [b"original"] * 3
    assert sleeps == [2, 4]


async def test_upload_retry_limit_and_deadline(tmp_path, monkeypatch):
    from app.runner import client as runner_client

    requests = []

    async def sleep(_seconds):
        pass

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(runner_client.asyncio, "sleep", sleep)
    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await client.upload(_task(), "output.pdf", b"pdf", "application/pdf")
    assert len(requests) == 3
    requests.clear()
    with pytest.raises(TimeoutError):
        await client.upload(
            _task(deadline=int((time.time() + 1) * 1000)), "output.pdf", b"pdf", "application/pdf"
        )
    assert len(requests) == 1
    requests.clear()
    with pytest.raises(TimeoutError):
        await client.upload(
            _task(deadline=int((time.time() - 1) * 1000)), "output.pdf", b"pdf", "application/pdf"
        )
    assert requests == []
    await client.close()


async def test_upload_deadline_cancels_sending():
    cancelled = asyncio.Event()

    async def handler(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = WorkerClient(WORKER, "host-token", transport=httpx.MockTransport(handler))
    with pytest.raises(TimeoutError):
        await client.upload(
            _task(deadline=int((time.time() + 0.05) * 1000)),
            "output.pdf",
            b"pdf",
            "application/pdf",
        )
    await client.close()
    assert cancelled.is_set()


def test_outcome_rejects_empty_or_contradictory_results():
    with pytest.raises(ValueError):
        runner_tasks.Outcome()
    with pytest.raises(ValueError):
        runner_tasks.Outcome(failed="failed", review_refused="refused")


@pytest.mark.parametrize("current", ["accepting", "stopping", "stopped"])
def test_claim_loop_guard_all_transitions(current):
    from app.runner.__main__ import RunnerControl, RunnerMode

    for target in RunnerMode:
        control = RunnerControl()
        control.mode = RunnerMode(current)
        if (current, target.value) in {("accepting", "stopping"), ("stopping", "stopped")}:
            control.transition(target)
            assert control.mode is target
        else:
            with pytest.raises(ValueError):
                control.transition(target)


@pytest.mark.parametrize("slot_available", [True, False])
async def test_stop_while_waiting_for_slot_never_acquires_work(slot_available):
    from app.runner.__main__ import acquire_slot

    slots = asyncio.Semaphore(1 if slot_available else 0)
    stop = asyncio.Event()
    pending = asyncio.create_task(acquire_slot(slots, stop))
    stop.set()
    assert not await asyncio.wait_for(pending, 1)
    assert slots.locked() is not slot_available


async def test_claim_loop_recovers_malformed_claim_and_does_not_dispatch_after_stop(
    tmp_path, monkeypatch
):
    from app.runner import __main__ as runner_main

    signals = []
    dispatched = []
    settings = _settings(tmp_path)

    class Client:
        base_url = WORKER
        calls = 0
        closed = False

        async def claim(self, _runner):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("malformed claim")
            signals[0]()
            return _task()

        async def close(self):
            self.closed = True

    async def sleep(*_args):
        pass

    async def dispatch(*args):
        dispatched.append(args)

    client = Client()
    monkeypatch.setattr(runner_main, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_main, "ensure_dirs", lambda: None)
    monkeypatch.setattr(runner_main, "WorkerClient", lambda *_args: client)
    monkeypatch.setattr(runner_main, "_sleep_unless", sleep)
    monkeypatch.setattr(runner_main, "run_task", dispatch)
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "add_signal_handler",
        lambda _signal, callback: signals.append(callback),
    )
    await asyncio.wait_for(runner_main.main(), 1)
    assert client.calls == 2
    assert client.closed
    assert dispatched == []


@pytest.mark.parametrize(
    "field,value",
    [("lease", None), ("job_id", ""), ("attempt", 0), ("deadline", "123"), ("kind", "unknown")],
)
def test_claim_protocol_rejects_malformed_fields(field, value):
    data = {
        "job_id": JOB_ID,
        "kind": "process",
        "lease": "lease",
        "attempt": 1,
        "deadline": 1000,
        "original_filename": "x.pdf",
    }
    with pytest.raises(ValueError):
        Task.from_json({**data, field: value})


async def test_attempt_paths_and_archives_work_in_both_runner_layouts(tmp_path):
    import sqlite3

    from app.config import get_settings, task_settings
    from app.runner.state import attempt_settings, data_root

    base = _settings(tmp_path)
    legacy = await _saved_state(base)
    first = attempt_settings(base, "old-lease")
    second = attempt_settings(base, "new-lease")
    archive = tmp_path / "legacy.tar.gz"
    archive.write_bytes(legacy)
    unpack_state(first, JOB_ID, archive)
    unpack_state(second, JOB_ID, archive)
    with task_settings(first):
        assert get_settings().processing_dir == first.processing_dir
        assert (
            await asyncio.to_thread(lambda: get_settings().processing_dir) == first.processing_dir
        )
    assert database_path(first, JOB_ID) != database_path(second, JOB_ID)
    with sqlite3.connect(database_path(second, JOB_ID)) as db:
        row = db.execute("SELECT input_path, output_path FROM jobs").fetchone()
        assert all(str(data_root(second)) in path for path in row)
        assert all(Path(path).exists() for path in row)
    modern = pack_state(second, JOB_ID, tmp_path / "modern.tar.gz", canonical_root=data_root(base))
    # The deployed unpacker only checks member names and extracts; it does no rebasing.
    with tarfile.open(modern) as tar:
        tar.extractall(data_root(base), filter="data")
    with sqlite3.connect(database_path(base, JOB_ID)) as db:
        row = db.execute("SELECT input_path, output_path FROM jobs").fetchone()
        assert all(str(data_root(base)) in path for path in row)
        assert all(Path(path).exists() for path in row)
    remove_job_files(first, JOB_ID)
    assert input_path(second, JOB_ID).exists()


@pytest.mark.parametrize("cancel_again", [False, True])
async def test_cancelled_attempt_joins_thread_before_removing_its_files(
    tmp_path, monkeypatch, cancel_again
):
    from app.runner.state import attempt_settings
    from app.services.thread_work import to_thread

    settings = _settings(tmp_path)
    task = _task()
    scoped = attempt_settings(settings, task.lease)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work():
        path = input_path(scoped, JOB_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned")
        started.set()
        assert release.wait(3)
        assert path.read_bytes() == b"owned"
        path.write_bytes(b"finished")
        finished.set()

    async def perform(*_args):
        await to_thread(work)
        raise AssertionError("cancelled work must not publish")

    monkeypatch.setattr(runner_tasks, "_perform", perform)
    client = FakeWorker().client()
    running = asyncio.create_task(runner_tasks.run_task(client, task, settings))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        running.cancel()
        await asyncio.sleep(0.02)
        assert not running.done()
        assert input_path(scoped, JOB_ID).exists()
        if cancel_again:
            running.cancel()
            await asyncio.sleep(0.02)
            assert not running.done()
            assert input_path(scoped, JOB_ID).exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert finished.is_set()
        assert not input_path(scoped, JOB_ID).exists()
    finally:
        release.set()
        await client.close()


@pytest.mark.parametrize("current", list(runner_tasks.TaskPhase))
def test_execution_phase_all_transitions(tmp_path, current):
    allowed = {
        "claimed": {"working", "failed"},
        "working": {"publishing", "failed"},
        "publishing": {"done", "failed"},
        "done": set(),
        "failed": set(),
    }
    for target in runner_tasks.TaskPhase:
        execution = runner_tasks.ClaimedTaskExecution(None, _task(), _settings(tmp_path))
        execution.phase = current
        if target.value in allowed[current.value]:
            execution.transition(target)
            assert execution.phase is target
        else:
            with pytest.raises(ValueError):
                execution.transition(target)


@pytest.mark.parametrize("body", [[], {"task": None}, {"task": []}, {"task": "bad"}, {}])
async def test_claim_rejects_malformed_envelopes_without_attribute_errors(body):
    client = WorkerClient(
        WORKER,
        "host-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(ValueError):
            await client.claim("runner")
    finally:
        await client.close()


def test_startup_removes_crashed_attempts_but_preserves_a_live_file_owner(tmp_path):
    from app.runner.state import (
        attempt_settings,
        attempt_workspace,
        cleanup_abandoned_attempts,
        data_root,
    )

    settings = _settings(tmp_path)
    crashed = data_root(attempt_settings(settings, "crashed"))
    crashed.mkdir(parents=True)
    (crashed / "orphan.pdf").write_bytes(PDF)
    with attempt_workspace(settings, "live") as live:
        path = input_path(live, JOB_ID)
        path.parent.mkdir(parents=True)
        path.write_bytes(PDF)
        cleanup_abandoned_attempts(settings)
        assert not crashed.exists()
        assert path.exists()
        with pytest.raises(RuntimeError):
            with attempt_workspace(settings, "live"):
                raise AssertionError("duplicate file owner")
    assert not data_root(live).exists()
