import { readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const mutations = [
  ["upload retry selection", "src/state/upload.ts", 'outcome.status === "selected" || outcome.status === "failed"', 'outcome.status !== "uploading"', "retries a partial batch"],
  ["upload pending guard", "src/state/upload.ts", 'state.requestId !== null || retryableUploads(state).length === 0', 'retryableUploads(state).length === 0', "rejects double submit"],
  ["upload result guard", "src/state/upload.ts", 'if (item.outcome.status !== "uploading" || item.outcome.requestId !== event.requestId) throw illegal();', 'if (false) throw illegal();', "guards each per-file write"],
  ["review serialization", "src/state/reviewAction.ts", 'state.status === "pending" || !event.permitted', '!event.permitted', "rejects overlapping"],
  ["review permission", "src/state/reviewAction.ts", 'state.status === "pending" || !event.permitted', 'state.status === "pending"', "refuses non-reviewable"],
  ["review request identity", "src/state/reviewAction.ts", 'state.status === "idle" || event.requestId !== state.requestId', 'state.status === "idle"', "ignores stale successes"],
  ["editor freeze", "src/state/editor.ts", 'case "type":\n      if (state.status !== "drafting" && state.status !== "failed") throw illegal();', 'case "type":\n      if (false) throw illegal();', "freezes typing"],
  ["editor revision", "src/state/editor.ts", ' || state.revision !== event.revision', '', "refuses stale revision"],
  ["delete pending cancel", "src/state/deleteConfirmation.ts", 'deleting: ["deleted", "failed"]', 'deleting: ["deleted", "failed", "cancel"]', "deleting -> cancel is guarded"],
  ["native dialog cancel", "src/components/ConfirmDialog.tsx", '        event.preventDefault();', '        // mutant: allow native dismissal', "keeps .* dialog visible"],
  ["image identity", "src/state/image.ts", 'if (event.source !== state.source || event.attempt !== state.attempt) return state;', 'if (false) return state;', "retries failed source"],
  ["toast timeout identity", "src/state/toast.ts", 'state.status !== "visible" || state.id !== event.id', 'state.status !== "visible"', "replacement and dismiss"],
  ["context identity", "src/state/jobView.ts", '!query.data || query.data.revision !== revision', '!query.data', "never treats absent or failed validation"],
  ["snapshot identity", "src/state/jobView.ts", '!outputReady(after) || jobRevision(after) !== revision', '!outputReady(after)', "rejects revision skew"],
  ["download capability", "src/state/jobView.ts", 'const ready = outputReady(job);', 'const ready = true;', "projects .* without writing remote lifecycle"],
  ["compliance evidence", "src/state/jobView.ts", 'status === "complete" && validated && context === "ready"', 'status === "complete" && context === "ready"', "does not equate a complete job"],
  ["Doorway redirect boundary", "src/state/request.ts", ' && launch.startsWith("/launch/")', '', "only accepts a Doorway launch redirect"],
  ["filter write guard", "src/state/modes.ts", 'if (next !== undefined && !JOB_STATUSES.includes(next))', 'if (false)', "permits all known filters"],
  ["late picker guard", "src/state/dropzone.ts", 'if (disabled) return;', 'if (false) return;', "projects disabled state over dragging"],
  ["toast registration identity", "src/state/toast.ts", 'if (callback === next) callback = null;', 'callback = null;', "unregisters only its own callback"],
];
let failures = 0;
let restore = null;
const cleanup = () => { if (restore) { writeFileSync(restore.path, restore.source); restore = null; } };
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => { cleanup(); process.exit(1); });
try {
  for (const [name, relative, before, after, test] of mutations) {
    const path = root + relative;
    const source = readFileSync(path, "utf8");
    if (source.split(before).length !== 2 || before === after) {
      console.error(`PATTERN_FAILED ${name}`); failures++; continue;
    }
    const baseline = spawnSync("node", ["node_modules/vitest/vitest.mjs", "run", "-t", test], { cwd: root, encoding: "utf8" });
    if (baseline.status !== 0 || !/Tests\s+\d+ passed/.test(baseline.stdout)) {
      console.error(`BASELINE_FAILED ${name}\n${baseline.stdout}\n${baseline.stderr}`); failures++; continue;
    }
    restore = { path, source };
    writeFileSync(path, source.replace(before, after));
    const result = spawnSync("node", ["node_modules/vitest/vitest.mjs", "run", "-t", test], { cwd: root, encoding: "utf8" });
    cleanup();
    if (result.status !== 0 && /Tests\s+\d+ failed/.test(result.stdout)) console.log(`KILLED ${name}`);
    else { console.error(`SURVIVED_OR_INVALID ${name}\n${result.stdout}\n${result.stderr}`); failures++; }
  }
} finally { cleanup(); }
process.exitCode = failures ? 1 : 0;
