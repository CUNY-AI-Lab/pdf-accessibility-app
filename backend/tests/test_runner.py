import asyncio
import io
import json
import tarfile
import time

import httpx
import pytest

from app.config import Settings
from app.models import AltTextEntry, AppliedChange, Job, ReviewTask
from app.runner import tasks as runner_tasks
from app.runner import views as runner_views
from app.runner.client import JOB_TOKEN_HEADER, RUNNER_TOKEN_HEADER, Task, WorkerClient
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

    def __init__(self, *, state: bytes | None = None, progress_status: int = 200):
        self.state = state
        self.progress_status = progress_status
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
async def test_process_task_runs_through_the_relay_and_uploads_views(tmp_path, monkeypatch, quiet_previews):
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


async def _saved_state(settings) -> bytes:
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
    await engine.dispose()
    archive = pack_state(settings, JOB_ID, settings.upload_dir.parent / "saved.tar.gz")
    body = archive.read_bytes()
    archive.unlink()
    remove_job_files(settings, JOB_ID)
    return body


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
async def test_refused_review_action_keeps_the_previous_result(tmp_path, monkeypatch, quiet_previews):
    settings = _settings(tmp_path)
    worker = FakeWorker(state=await _saved_state(settings))
    client = worker.client()

    async def fake_rerun(*_args):
        raise AssertionError("a refused action must not re-run tagging")

    monkeypatch.setattr(runner_tasks, "run_tagging_and_validation", fake_rerun)
    task = _task("review", action={"type": "undo", "change_id": 1}, overrides={"changes": {"1": "kept"}})
    await runner_tasks.run_task(client, task, settings)
    await client.close()

    assert worker.uploads == {}
    assert worker.completed == {
        "review_refused": (
            "This figure decision is no longer pending review. "
            "Refresh to see the latest job state."
        ),
        "overrides_version": 0,
    }


@pytest.mark.asyncio
async def test_lost_lease_stops_the_pipeline(tmp_path, monkeypatch, quiet_previews):
    settings = _settings(tmp_path)
    worker = FakeWorker(progress_status=409)
    client = worker.client()
    cancelled = asyncio.Event()

    async def slow_pipeline(job_id, session_maker, settings_arg, sink):
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
