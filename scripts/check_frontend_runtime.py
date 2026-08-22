#!/usr/bin/env python3
"""Exercise the built frontend through a real browser boundary.

The check intentionally starts from the HTML document, discovers its actual
script and stylesheet references, fetches each one, and verifies that React
rendered visible content. It is useful against both a local container and the
public deployment:

    python scripts/check_frontend_runtime.py https://tools.example/pdf-accessibility/
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def _asset_urls(page) -> list[str]:
    return page.locator("script[src], link[rel~='stylesheet'][href]").evaluate_all(
        """
        (elements) => elements
          .map((element) => element.src || element.href)
          .filter((url) => url)
        """
    )


def check(url: str) -> None:
    expected_origin = urlparse(url).netloc
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "requestfailed",
                lambda request: failed_requests.append(f"{request.url}: {request.failure}"),
            )

            response = page.goto(url, wait_until="networkidle", timeout=30_000)
            if response is None or not 200 <= response.status < 300:
                status = response.status if response is not None else "no response"
                raise AssertionError(f"frontend document returned {status}: {url}")

            asset_urls = _asset_urls(page)
            app_asset_urls = [
                asset_url
                for asset_url in asset_urls
                if urlparse(asset_url).netloc == expected_origin
            ]
            if not app_asset_urls:
                raise AssertionError("frontend document referenced no same-origin assets")

            for asset_url in app_asset_urls:
                asset_response = page.request.get(asset_url, timeout=30_000)
                if not 200 <= asset_response.status < 300:
                    raise AssertionError(
                        f"frontend asset returned {asset_response.status}: {asset_url}"
                    )
                if not asset_response.body():
                    raise AssertionError(f"frontend asset was empty: {asset_url}")

            root = page.locator("#root")
            if not root.is_visible():
                raise AssertionError("#root is not visible after frontend load")
            if root.locator(":scope > *").count() == 0:
                raise AssertionError("React rendered no direct child under #root")
            if not page.locator("body").inner_text().strip():
                raise AssertionError("frontend body is blank after rendering")
            if console_errors or page_errors or failed_requests:
                details = console_errors + page_errors + failed_requests
                raise AssertionError("browser reported errors: " + " | ".join(details))
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="frontend URL, including its deployment base path")
    args = parser.parse_args()
    try:
        check(args.url)
    except (AssertionError, PlaywrightError) as error:
        print(f"frontend runtime check failed: {error}", file=sys.stderr)
        return 1
    print(f"frontend runtime check passed: {urljoin(args.url, '.')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
