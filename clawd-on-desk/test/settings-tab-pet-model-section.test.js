"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const SRC_DIR = path.join(__dirname, "..", "src");

// ── Regression guards ─────────────────────────────────────────────────
// Incident (2026-08-30): a model switch + slow local chat left the
// Electron MAIN process hung (Windows Application Hang, all windows
// ghosted). Root contributor: window.alert() in settings renderer code —
// in Electron, alert() blocks the main process until dismissed.
//
// 2026-09 redesign: the model page was restructured into ONE picker row
// (current model + path + actions), a collapsed scanned-folders
// disclosure, a switch-driven engine runtime row, and the engine install
// location moved into Advanced. These tests lock that shape so a future
// edit doesn't quietly re-stack the wall of rows.

test("settings-tab-pet.js never calls window.alert (blocks the main process)", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  assert.doesNotMatch(code, /\balert\(/, "alert() must not be used — it ghosts every window; use notifyError (ops.showToast) instead");
  assert.match(code, /function notifyError\(/, "notifyError helper must exist");
  assert.match(code, /ops\.showToast\(/, "notifyError must route through ops.showToast");
});

test("model section is a hero card plus a lazy model switcher", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  // The read-only info row is gone (the engine section's own info row
  // is a different construct and must remain).
  assert.doesNotMatch(code, /const infoRow = el\(/);
  // The hero card carries the canonical "current model" display: name,
  // live status badge, full path (ellipsis + title tooltip) and actions.
  assert.match(code, /className: "section-rows pet-model-hero"/);
  assert.match(code, /pet-model-hero-status tone-\$/);
  assert.match(code, /pickLabelFromPath\(modelDir\)/);
  assert.match(code, /t\("petChangeModel"\)/);
  // The standalone path row is gone; the hero shows the full path.
  assert.doesNotMatch(code, /className: "row pet-path-row"/);
  assert.match(code, /pet-model-hero-path/);
  // The duplicate-description picker label key is no longer used here.
  assert.doesNotMatch(code, /t\("petModelPickerLabel"\)/);
  // The switcher card is built unappended and only inserted when the
  // scan finds at least two models — no flash of an empty picker.
  assert.match(code, /t\("petRowSwitchModel"\)/);
  assert.match(code, /box\.insertBefore\(switchSection, trigger\)/);
  assert.match(code, /models\.length < 2/);
  assert.match(code, /picker\.value = currentPath \|\| "";/);
});

test("scanned folders live behind a collapsed disclosure", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  assert.match(code, /let foldersExpanded = false;/);
  assert.match(code, /pet-folders-trigger/);
  assert.match(code, /pet-folders-body/);
  // Empty model list surfaces the folder manager automatically.
  assert.match(code, /foldersExpanded = true;/);
});

test("engine runtime is a switch and version actions stay on one row", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  assert.match(code, /switchRow\(\s*\n\s*t\("petRowEngineRunning"\)/);
  assert.match(code, /window\.petSettings\.engineStart : window\.petSettings\.engineStop/);
  // Engine failures must not read as "保存失败" — dedicated prefix.
  assert.match(code, /failureMessage: t\("petEngineStartFail"\)/);
  // Slow-network help is one collapsed link, not an always-visible row.
  assert.match(code, /t\("petEngineSlowLink"\)/);
  assert.doesNotMatch(code, /t\("petEngineSlowShow"\)/);
  assert.match(code, /pet-engine-progress-row/);
});

test("engine install location is debug info and lives in Advanced", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  const occurrences = code.split('t("petRowEngineLocation")').length - 1;
  assert.equal(occurrences, 1, "petRowEngineLocation must appear exactly once (in the Advanced section)");
  const advanced = code.slice(code.indexOf("function renderAdvancedSection"));
  assert.match(advanced, /t\("petRowEngineLocation"\)/);
});

test("health tick never rebuilds a card the user is interacting with", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  // An open <select> would snap shut if the tick rebuilt its card.
  assert.match(code, /function boxInteractionBusy\(/);
  assert.match(code, /if \(!boxInteractionBusy\(ctx\.modelBox\)\) renderModelSection/);
  assert.match(code, /if \(!boxInteractionBusy\(ctx\.engineBox\)\) await renderEngineSection/);
});

test("pet-chat.js loadModel allows long CPU model reloads", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "pet-chat.js"), "utf8");
  // /api/load-model restarts llama-server and waits for readiness; on a
  // CPU box under load this can exceed 90s, and a client-side timeout
  // turns into a reloadError → user-visible failure while the gateway
  // actually succeeds.
  assert.match(code, /\/api\/load-model`, body, 300000\)/);
});
