"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const RENDERER = path.join(__dirname, "..", "src", "pet-chat-renderer.js");
const HTML = path.join(__dirname, "..", "src", "pet-chat.html");

function read(file) { return fs.readFileSync(file, "utf8"); }

// The LifeLoop is the core continuous scheduler: it starts at boot, runs
// independently of the legacy proactive plugin / [NEXT_CHAT] / user presence,
// and is re-armed after every completed turn. Quiet rounds and errors only
// change the delay, never stop it.

test("LifeLoop is the one core scheduler and old continuous state is gone", () => {
  const code = read(RENDERER);
  assert.match(code, /const LIFE_PROMPT =/);
  assert.match(code, /function scheduleLifeLoop\(/);
  assert.match(code, /async function submitLifeRound\(/);
  assert.equal(
    (code.match(/async function submitLifeRound\(/g) || []).length,
    1,
    "exactly one submitLifeRound definition"
  );
  assert.match(code, /let _lifeEnabled = true;/);
  assert.doesNotMatch(code, /CONTINUOUS_/);
  assert.doesNotMatch(code, /_continuousFailures/);
  assert.doesNotMatch(code, /submitContinuous/);
  assert.doesNotMatch(code, /continuousGapSeconds/);
});

test("life rounds are silent until a real delta and submit re-arms the loop", () => {
  const code = read(RENDERER);
  assert.match(code, /const isLifeTurn = !!opts\.life;/);
  assert.match(code, /const isProactiveTurn = isLifeTurn \|\|/);
  assert.match(code, /if \(!isLifeTurn\) \{[\s\S]*?cancelLifeLoop\(\)/);
  assert.match(code, /if \(!isLifeTurn\) \{[\s\S]*?await showThinking/);
  assert.match(code, /await submit\(LIFE_PROMPT, \{ life: true \}\)/);
  assert.match(code, /scheduleLifeLoop\(\);\s*\n\s*\/\/ Only release the global handle/);
});

test("the legacy proactive scheduler is inert by default", () => {
  const code = read(RENDERER);
  const start = code.indexOf("function scheduleProactiveChat()");
  const end = code.indexOf("function setProactivePolicy", start);
  assert.ok(start !== -1 && end > start);
  const chunk = code.slice(start, end);
  assert.match(chunk, /if \(window\.__legacyProactiveScheduler !== true\) return;/);
  assert.doesNotMatch(chunk, /CONTINUOUS_|submitContinuous/);
});

test("policy and next_chat do not arm the old plugin", () => {
  const code = read(RENDERER);
  const policyStart = code.indexOf("function setProactivePolicy");
  const policyEnd = code.indexOf("function cancelProactiveChat", policyStart);
  assert.ok(policyStart !== -1 && policyEnd > policyStart);
  assert.doesNotMatch(code.slice(policyStart, policyEnd), /scheduleProactiveChat\(\)/);

  const nextStart = code.indexOf('obj.event === "next_chat"');
  const nextEnd = code.indexOf('obj.event === "end"', nextStart);
  assert.ok(nextStart !== -1 && nextEnd > nextStart);
  assert.doesNotMatch(code.slice(nextStart, nextEnd), /scheduleProactiveChat/);
});

test("the boot wake is the first life round even when proactive mode is off", () => {
  const code = read(RENDERER);
  const start = code.indexOf("function scheduleBootWake");
  const end = code.indexOf("// ── 身体感受", start);
  assert.ok(start !== -1 && end > start);
  const chunk = code.slice(start, end);
  assert.doesNotMatch(chunk, /__proactiveMode/);
  assert.match(chunk, /submit\(BOOT_WAKE_PROMPT, \{ life: true, allowTools: false \}\)/);
  assert.match(code, /opts\.allowTools === false\) body\.tools_enabled = false/);
  assert.doesNotMatch(chunk, /\[NEXT_CHAT:/);
});

test("errors back off but keep scheduling; a finished round resets the count", () => {
  const code = read(RENDERER);
  assert.match(code, /_lifeFailures \+= 1/);
  assert.match(code, /if \(_lifeFailures >= 2\) delay = Math\.max\(delay, LIFE_ERROR_BACKOFF_MS\)/);
  assert.match(code, /_lifeFailures = 0/); // SSE end
  assert.match(code, /showError\([\s\S]{0,200}scheduleLifeLoop\(\)/);
});

test("pet-chat.html no longer loads the retired continuous-life helper", () => {
  const html = read(HTML);
  assert.ok(!html.includes("continuous-life.js"));
  assert.ok(html.indexOf("pet-chat-renderer.js") > html.indexOf("pet-chat-context.js"));
});
