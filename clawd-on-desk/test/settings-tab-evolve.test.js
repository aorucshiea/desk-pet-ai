"use strict";

const { test, describe } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const SRC_DIR = path.join(__dirname, "..", "src");
const EVOLVE_SRC = path.join(SRC_DIR, "settings-tab-evolve.js");
const I18N_SRC = path.join(SRC_DIR, "settings-i18n.js");
const IPC_SRC = path.join(SRC_DIR, "settings-ipc.js");
const PRELOAD_SRC = path.join(SRC_DIR, "preload-settings.js");

// ── Regression guards for the plugin-kernel settings page ────────────
// The Evolve tab renders the sidecar's cordis-style plugin kernel live:
// organ plugins, the coeffect waiting room (pending), the service table
// and the effect ledger. These tests lock that shape so a future edit
// doesn't quietly turn the page back into the old auto-review stub.

describe("settings-tab-evolve: plugin kernel page", () => {
  test("stays browser-script friendly", () => {
    const code = fs.readFileSync(EVOLVE_SRC, "utf8");
    assert.ok(!code.includes("require("), "must not use CommonJS require");
    assert.ok(!code.includes("module.exports"), "must not use CommonJS exports");
    assert.match(code, /root\.ClawdSettingsTabEvolve = \{ init \};/);
  });

  test("renders the kernel surfaces: hero, organs, parked, services, effects", () => {
    const code = fs.readFileSync(EVOLVE_SRC, "utf8");
    // Kernel hero carries live counts and an offline state.
    assert.match(code, /section-rows evl-hero/);
    assert.match(code, /evolveKernelOk/);
    assert.match(code, /evolveKernelOffline/);
    assert.match(code, /evolveOfflineHint/);
    // The four kernel surfaces, in order.
    const order = [
      "renderPlugins\\(parent, state\\)",
      "renderPending\\(parent, state\\)",
      "renderServices\\(parent, state\\)",
      "renderEffects\\(parent, state\\)",
    ].join("[\\s\\S]*?");
    assert.match(code, new RegExp(order));
    // Unload + rescan actions exist and refresh afterwards.
    assert.match(code, /pluginsUnload\(/);
    assert.match(code, /pluginsLoad\(""\)/, "rescan sends an empty name to force a full sync");
    assert.match(code, /refresh\(panel, \{ force: true \}\)/);
  });

  test("the kernel's own context is protected and observability is cheap", () => {
    const code = fs.readFileSync(EVOLVE_SRC, "utf8");
    // plugin_forge is the file-less self-evolution tool — no unload button.
    assert.match(code, /function isKernelCore\(name\) \{ return name === "plugin_forge"; \}/);
    // Polling skips work when the tab is hidden or not active, and a JSON
    // compare keeps unchanged data from rebuilding the DOM.
    assert.match(code, /core\.state\.activeTab !== "evolve"/);
    assert.match(code, /document\.hidden/);
    assert.match(code, /json === lastStateJson/);
  });

  test("every evolve i18n key exists in all five product languages", () => {
    const tabCode = fs.readFileSync(EVOLVE_SRC, "utf8");
    const i18nCode = fs.readFileSync(I18N_SRC, "utf8");
    const used = [...new Set([...tabCode.matchAll(/t\("(evolve[A-Za-z]+)"/g)].map((m) => m[1]))];
    assert.ok(used.length >= 30, `expected the full evolve keyset, found ${used.length}`);

    // Slice PET_PRODUCT_STRINGS into per-language blocks and require each
    // key per block — the en fallback merge would otherwise hide gaps.
    const start = i18nCode.indexOf("const PET_PRODUCT_STRINGS = {");
    assert.ok(start > 0, "PET_PRODUCT_STRINGS block found");
    const section = i18nCode.slice(start);
    const markers = ["en", "zh", '"zh-TW"', "ko", "ja"].map(
      (lang) => `    ${lang}: {`
    );
    const bounds = markers.map((m) => section.indexOf(m));
    assert.ok(bounds.every((b) => b > 0), "all five language blocks found");
    bounds.push(section.length);
    for (let i = 0; i < markers.length; i++) {
      const block = section.slice(bounds[i], bounds[i + 1]);
      for (const key of used) {
        assert.match(block, new RegExp(`\\b${key}: "`), `${key} missing in ${markers[i].trim()}`);
      }
    }
  });

  test("sidecar bridge wires state/unload/load through settings IPC + preload", () => {
    const ipc = fs.readFileSync(IPC_SRC, "utf8");
    for (const channel of ["settings:plugins-state", "settings:plugins-unload", "settings:plugins-load"]) {
      assert.match(ipc, new RegExp(`handle\\("${channel}"`), `${channel} must be registered`);
    }
    assert.match(ipc, /sidecarJson\("GET", "\/api\/plugins", 4000\)/);
    assert.match(ipc, /sidecarJson\("POST", "\/api\/plugins\/unload", 8000, \{ name \}\)/);
    assert.match(ipc, /sidecarJson\("POST", "\/api\/plugins\/load", 15000, body\)/);

    const preload = fs.readFileSync(PRELOAD_SRC, "utf8");
    assert.match(preload, /pluginsState: \(\) => ipcRenderer\.invoke\("settings:plugins-state"\)/);
    assert.match(preload, /pluginsUnload: \(name\) => ipcRenderer\.invoke\("settings:plugins-unload", \{ name \}\)/);
    assert.match(preload, /pluginsLoad: \(name\) => ipcRenderer\.invoke\("settings:plugins-load", \{ name \}\)/);
  });

  test("config editing consumes the plugins/config contract and degrades silently", () => {
    const tabCode = fs.readFileSync(EVOLVE_SRC, "utf8");
    const ipc = fs.readFileSync(IPC_SRC, "utf8");
    const preload = fs.readFileSync(PRELOAD_SRC, "utf8");
    // Contract shape from the kernel session: GET → {plugins: {name: {schema, values}}},
    // POST → {name, values}; editors render only when the endpoint answers.
    assert.match(ipc, /handle\("settings:plugins-config"/);
    assert.match(ipc, /sidecarJson\("GET", "\/api\/plugins\/config", 4000\)/);
    assert.match(ipc, /sidecarJson\("POST", "\/api\/plugins\/config", 15000, \{ name, values \}\)/);
    assert.match(preload, /pluginsConfig: \(\) => ipcRenderer\.invoke\("settings:plugins-config"\)/);
    assert.match(preload, /pluginsSetConfig: \(name, values\) => ipcRenderer\.invoke\("settings:plugins-set-config", \{ name, values \}\)/);
    assert.match(tabCode, /renderConfig\(parent, state\)/);
    assert.match(tabCode, /state\.configData && state\.configData\.plugins/);
    assert.match(tabCode, /configInput\(entry, values\[key\]\)/);
    // The 5s poll must not wipe in-progress edits: focus guard before rebuild.
    assert.match(tabCode, /\["INPUT", "SELECT", "TEXTAREA", "BUTTON"\]\.includes\(active\.tagName\)/);
  });
});
