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
// 2026-10 restructure (机长: "这个模块目的是让用户知道大脑是哪个"):
// the 大脑 page's 模型 card became read-only. Picking a model, opening its
// folder and managing scan roots moved to 模型来源 → 本地模型, together with
// the 推理引擎 card and 高级设置. These tests lock that split.

test("settings-tab-pet.js never calls window.alert (blocks the main process)", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  assert.doesNotMatch(code, /\balert\(/, "alert() must not be used — it ghosts every window; use notifyError (ops.showToast) instead");
  assert.match(code, /function notifyError\(/, "notifyError helper must exist");
  assert.match(code, /ops\.showToast\(/, "notifyError must route through ops.showToast");
});

test("模型 card reports the brain and offers no actions", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  assert.match(code, /className: "section-rows pet-model-hero"/);
  assert.match(code, /pet-model-hero-status tone-\$/);
  assert.match(code, /pickLabelFromPath\(modelDir\)/);
  // Full path with CSS ellipsis + restoring tooltip, never char truncation.
  assert.match(code, /pet-model-hero-path/);
  assert.doesNotMatch(code, /className: "row pet-path-row"/);
  assert.doesNotMatch(code, /pet-model-switch-row/);
  assert.doesNotMatch(code, /pet-model-hero-actions/);
  // The two buttons that used to sit under the hero are gone for good:
  // "在文件夹中显示" opened a folder, "更换…" picked a model — neither
  // answers "which brain is this".
  assert.doesNotMatch(code, /window\.petSettings\.openModelDir/);
  assert.doesNotMatch(code, /window\.petSettings\.pickModelDir/);
  assert.doesNotMatch(code, /t\("petChangeModel"\)/);
  assert.doesNotMatch(code, /modelPathOpenLabel/);
  // While the engine is stopped the health payload carries no model at all,
  // so the hero is filled from the configured model instead of falsely
  // reading 未选择模型.
  assert.match(code, /heroName\.textContent = cur\.label/);
});

test("模型 card names a non-local brain too — it is not a local-only panel", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  // Reads the live provider out of the shared settings snapshot.
  assert.match(code, /function activeBrainSource\(/);
  assert.match(code, /skills\.defaultProvider/);
  assert.match(code, /modelProviders/);
  // Used by the hero (name line, detail line and the source chip), and the
  // local-model fill must not overwrite a cloud brain.
  assert.match(code, /const src = activeBrainSource\(\)/);
  assert.match(code, /src\.name \|\| t\("petModelPathUnset"\)/);
  assert.match(code, /if \(hasPath \|\| src\.name \|\| !heroName\.isConnected\) return;/);
});

test("model choice and scan folders live in 模型来源 → 本地模型", () => {
  const pet = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  const providers = fs.readFileSync(path.join(SRC_DIR, "settings-tab-providers.js"), "utf8");

  // The Brain page no longer hosts the folder manager.
  assert.doesNotMatch(pet, /pet-folders-trigger/);
  assert.doesNotMatch(pet, /let foldersExpanded/);

  // The local source pane does: switcher, browse, folder list + add/remove.
  assert.match(providers, /function localModelBlock\(/);
  assert.match(providers, /d\.appendChild\(localModelBlock\(\)\)/);
  assert.match(providers, /window\.petSettings\.useModelDir\(/);
  assert.match(providers, /window\.petSettings\.pickModelDir\(/);
  assert.match(providers, /window\.petSettings\.listLocalModels\(/);
  assert.match(providers, /window\.petSettings\.listModelFolders\(/);
  assert.match(providers, /window\.petSettings\.removeModelFolder\(/);
  assert.match(providers, /window\.petSettings\.addModelFolder\(/);
  assert.match(providers, /window\.petSettings\.addModelFile\(/);
});

test("推理引擎 + 高级设置 are adopted by 模型来源, not painted on 大脑", () => {
  const pet = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  const providers = fs.readFileSync(path.join(SRC_DIR, "settings-tab-providers.js"), "utf8");

  // The Brain page appends only its header and the model card.
  assert.doesNotMatch(pet, /parent\.appendChild\(ctx\.engineBox\)/);
  assert.doesNotMatch(pet, /parent\.appendChild\(ctx\.advancedBox\)/);

  // The cards are owned by the Brain module (its health poll feeds them) and
  // handed to the providers page through core.enginePanel.
  assert.match(pet, /core\.enginePanel = \{ mount: mountEnginePanel \}/);
  assert.match(pet, /function mountEnginePanel\(host\)/);
  assert.match(pet, /host\.appendChild\(ctx\.engineBox\)/);
  assert.match(pet, /host\.appendChild\(ctx\.advancedBox\)/);
  assert.match(providers, /core\.enginePanel\.mount\(engineHost\)/);
  // A dead `if (false && …)` switch would silently drop the whole card.
  assert.doesNotMatch(providers, /if \(false &&/);

  // Page state must outlive one render, and the poll must survive on either
  // page, or the moved cards freeze the moment the user leaves 大脑.
  assert.match(pet, /let pageCtx = null;/);
  assert.match(pet, /function ensureCtx\(\)/);
  assert.match(pet, /tab === "pet" \|\| tab === "providers"/);
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
  assert.match(advanced, /t\("petActionRestartSidecar"\)/);
  assert.match(advanced, /t\("petActionOpenLogs"\)/);
});

test("health tick never rebuilds a card the user is interacting with", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-pet.js"), "utf8");
  // An open <select> would snap shut if the tick rebuilt its card.
  assert.match(code, /function boxInteractionBusy\(/);
  assert.match(code, /if \(!boxInteractionBusy\(ctx\.modelBox\)\) renderModelSection/);
  assert.match(code, /!boxInteractionBusy\(ctx\.engineBox\)/);
  // The engine + advanced cards live on another page: painting a detached
  // box would leak an orphan card and lose the mounted one's state.
  assert.match(code, /ctx\.engineBox\.isConnected/);
  assert.match(code, /ctx\.advancedBox\.isConnected/);
});

test("pet-chat.js loadModel allows long CPU model reloads", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "pet-chat.js"), "utf8");
  // /api/load-model restarts llama-server and waits for readiness; on a
  // CPU box under load this can exceed 90s, and a client-side timeout
  // turns into a user-visible failure while the gateway actually succeeds.
  assert.match(code, /\/api\/load-model`, body, 300000\)/);
});
