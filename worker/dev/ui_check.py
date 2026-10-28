"""Use the app in a browser against a local Worker, as a person would.

Run by dev/e2e.mjs with E2E_BROWSER=1. The identity headers stand in for
Doorway. It uploads a PDF through the upload page, follows the job page until
the result appears, and fetches the accessible PDF its link points to.

    E2E_IDENTITY_HEADERS='{...}' uv run python ../worker/dev/ui_check.py BASE_URL file.pdf
"""

import json
import os
import re
import sys
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright


def main(base: str, pdf: str) -> str:
    headers = json.loads(os.environ["E2E_IDENTITY_HEADERS"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(extra_http_headers=headers, accept_downloads=True)
        page = context.new_page()
        problems: list[str] = []
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on(
            "response",
            lambda response: problems.append(f"{response.status} {response.url}")
            if response.status >= 400 and "/api/" in response.url
            else None,
        )

        page.goto(f"{base}/")
        expect(page.get_by_role("heading", name="Upload PDF documents")).to_be_visible()
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name=re.compile("Upload PDF documents")).click()
        chooser.value.set_files(pdf)
        page.get_by_role("button", name=re.compile("Start Processing")).click()

        page.wait_for_url(re.compile(r"/pdf-accessibility/jobs/[0-9a-f-]{36}$"), timeout=60_000)
        job_id = page.url.rsplit("/", 1)[-1]
        expect(page.get_by_role("heading", name="Pipeline Progress")).to_be_visible()
        download = page.get_by_role("link", name="Download Accessible PDF")
        expect(download.first).to_be_visible(timeout=20 * 60_000)

        # Chromium's download path leaves out the injected identity headers
        # (Doorway adds them from the session in production), so follow the
        # link's target with the same headers instead of clicking it.
        href = download.first.get_attribute("href") or ""
        if not href.endswith(f"/api/jobs/{job_id}/download"):
            problems.append(f"the download link points at {href!r}")
        response = context.request.get(urljoin(page.url, href))
        if response.status != 200 or response.body()[:5] != b"%PDF-":
            problems.append(f"the download link answered {response.status} without a PDF")
        browser.close()
    if problems:
        raise SystemExit("browser check failed:\n  " + "\n  ".join(problems))
    return job_id


if __name__ == "__main__":
    print(main(sys.argv[1], sys.argv[2]))
