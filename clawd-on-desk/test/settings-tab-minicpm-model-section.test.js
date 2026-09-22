"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const SRC_DIR = path.join(__dirname, "..", "src");

// ── 2026-08-30 regression guards ─────────────────────────────────────
// Incident: a model switch + slow local chat left the Electron MAIN
// process hung (Windows Application Hang, all windows ghosted). Root
// contributor: window.alert() in settings renderer code — in Electron,
// alert() blocks the main process until dismissed. Also: the MiniCPM
// model section had a read-only info row duplicating the picker's
// description text, reading like a second switcher.

test("settings-tab-minicpm.js never calls window.alert (blocks the main process)", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-minicpm.js"), "utf8");
  assert.doesNotMatch(code, /\balert\(/, "alert() must not be used — it ghosts every window; use notifyError (ops.showToast) instead");
  assert.match(code, /function notifyError\(/, "notifyError helper must exist");
  assert.match(code, /ops\.showToast\(/, "notifyError must route through ops.showToast");
});

test("settings-tab-minicpm.js model section merges info row into the picker row", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-minicpm.js"), "utf8");
  // The read-only info row is gone (the engine section's own info row
  // is a different construct and must remain).
  assert.doesNotMatch(code, /const infoRow = el\(/);
  // The picker row carries the canonical "模型" label and pre-selects
  // the current model (its ✓ flag doubles as the info display).
  assert.match(code, /className: "row minicpm-model-picker-row"/);
  assert.match(code, /t\("minicpmRowModelInfo"\)/);
  assert.match(code, /const currentPath = /);
  assert.match(code, /picker\.value = currentPath \|\| "";/);
  // The duplicate-description picker label key is no longer used here.
  assert.doesNotMatch(code, /t\("minicpmModelPickerLabel"\)/);
});

test("minicpm-chat.js loadModel allows long CPU model reloads", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "minicpm-chat.js"), "utf8");
  // /api/load-model restarts llama-server and waits for readiness; on a
  // CPU box under load this can exceed 90s, and a client-side timeout
  // turns into a reloadError → user-visible failure while the gateway
  // actually succeeds.
  assert.match(code, /\/api\/load-model`, body, 300000\)/);
});
