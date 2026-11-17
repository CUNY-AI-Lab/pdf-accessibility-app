// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it } from "vitest";
import Layout from "./Layout";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); });

// `pdf` is the topic key cail-ticketing maps to this app; an unknown key still
// opens the form, just with nothing chosen.
it("footer links to CAIL Ticketing with PDF Accessibility chosen", () => {
  act(() => root.render(<MemoryRouter><Layout /></MemoryRouter>));
  const link = [...host.querySelectorAll("footer a")].find((item) => item.textContent?.trim() === "Report a problem");
  expect(link?.getAttribute("href")).toBe("https://tools.ailab.gc.cuny.edu/help/projects/support/issues/new?topic=pdf");
  expect(link?.getAttribute("target")).toBe("_blank");
  expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
});
