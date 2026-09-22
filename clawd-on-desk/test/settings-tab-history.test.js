"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const SRC_DIR = path.join(__dirname, "..", "src");
const { SUPPORTED_LANGS } = require("../src/i18n");

// ── Settings → 历史 (24h conversation history viewer) ─────────────────
// Data lives in chat-history.json (Electron side, B-12); the viewer is a
// settings tab that reads the FILE (works even when the bubble is closed)
// and filters to the last 24 hours. Thinking is collapsed by default —
// the user complaint that motivated this tab was unreadably long
// thinking output.

test("settings-tab-history.js loads via the sibling IIFE pattern and registers the tab", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-history.js"), "utf8");
  assert.match(code, /root\.ClawdSettingsTabHistory\s*=\s*\{\s*init\s*\}/);
  assert.match(code, /core\.tabs\["history"\]\s*=|core\.tabs\.history\s*=/);
});

test("settings.html includes settings-tab-history.js before settings-renderer.js", () => {
  const html = fs.readFileSync(path.join(SRC_DIR, "settings.html"), "utf8");
  const tabIdx = html.indexOf("settings-tab-history.js");
  const rendererIdx = html.indexOf("settings-renderer.js");
  assert.ok(tabIdx > 0, "settings-tab-history.js must appear in settings.html");
  assert.ok(rendererIdx > tabIdx, "settings-renderer.js must come after settings-tab-history.js");
});

test("settings-renderer.js SIDEBAR_TABS includes the history entry", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-renderer.js"), "utf8");
  assert.match(code, /id:\s*"history"/);
  assert.match(code, /labelKey:\s*"sidebarHistory"/);
});

test("history data plumbing: file-backed IPC + preload exposure", () => {
  const main = fs.readFileSync(path.join(SRC_DIR, "minicpm-chat.js"), "utf8");
  assert.match(main, /minicpm-settings:get-history-file/);
  assert.match(main, /CHAT_HISTORY_PATH/, "viewer must read the persisted file, not bubble memory");
  const preload = fs.readFileSync(path.join(SRC_DIR, "preload-settings.js"), "utf8");
  assert.match(preload, /getHistoryFile:\s*\(\)\s*=>\s*ipcRenderer\.invoke\("minicpm-settings:get-history-file"\)/);
});

test("persisted messages carry timestamps + thinking for the 24h filter", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "minicpm-chat-renderer.js"), "utf8");
  // _cleanHistoryForSave stamps ts once per live message (re-saves keep
  // the original stamp) and keeps thinking for the collapsed block.
  assert.match(code, /if \(m && typeof m === "object" && !m\.ts\) m\.ts = now;/);
  assert.match(code, /entry\.thinking = String\(m\.thinking\)/);
});

test("settings-i18n.js: all language packs include the history keys", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-i18n.js"), "utf8");
  const KEYS = [
    "sidebarHistory",
    "historyTitle",
    "historySubtitle",
    "historyRefresh",
    "historyEmpty",
    "historyNoTs",
    "historyThinking",
    "historyProactive",
  ];
  for (const key of KEYS) {
    const matches = code.match(new RegExp(`\\b${key}\\b`, "g")) || [];
    assert.ok(
      matches.length >= SUPPORTED_LANGS.length,
      `${key} must exist in all ${SUPPORTED_LANGS.length} language packs (found ${matches.length})`,
    );
  }
});

test("history tab never calls alert (main-process blocker, see B-16)", () => {
  const code = fs.readFileSync(path.join(SRC_DIR, "settings-tab-history.js"), "utf8");
  assert.doesNotMatch(code, /\balert\(/);
});
