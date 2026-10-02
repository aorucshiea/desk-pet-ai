"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

// The memory layer is now exactly two things (机长 2026-10-02): the model's
// own 「记忆」 folder, and the hardcoded prompt that tells it how to use it.
// That makes the prompt load-bearing: an instruction naming a surface that no
// longer exists is not a typo, it is the pet being told to do something that
// cannot work. 2ca7303 replaced MEMORY.md/USER.md with the folder tree and
// add/replace/remove with list/read/write/delete — the prompt still promised
// "replace/remove 旧的" and "写进 MEMORY.md 的对应部分".

const RENDERER = path.join(__dirname, "..", "src", "pet-chat-renderer.js");
const TOOL = path.join(__dirname, "..", "..", "pet-sidecar", "gateway", "memory", "tool.py");

function read(file) { return fs.readFileSync(file, "utf8"); }

test("the chat prompt no longer names the retired MEMORY.md / USER.md surfaces", () => {
  const code = read(RENDERER);
  assert.doesNotMatch(code, /MEMORY\.md|USER\.md/,
    "the identity-file pair is gone; the prompt must point at 记忆/ instead");
  assert.match(code, /【你的记忆文件夹】/);
  assert.match(code, /「记忆」文件夹/);
});

test("every memory action the prompt advertises exists in the tool schema", () => {
  const tool = read(TOOL);
  const enumLine = tool.match(/"enum":\s*\[([^\]]*)\]/);
  assert.ok(enumLine, "memory tool must declare its action enum");
  const actions = enumLine[1].split(",").map((s) => s.trim().replace(/"/g, "")).filter(Boolean);
  assert.deepEqual(actions, ["list", "read", "write", "delete"],
    "the tool's action set changed — update the prompt in the same commit");

  const code = read(RENDERER);
  for (const action of actions) {
    assert.ok(new RegExp(`\\b${action}\\b`).test(code),
      `prompt never mentions the '${action}' action the tool offers`);
  }
  // The retired verbs must not sneak back in as instructions.
  assert.doesNotMatch(code, /replace\/remove|remove 旧的/,
    "add/replace/remove were the MEMORY.md-era verbs; they no longer exist");
});

test("the memory block is read live from the new /api/memory shape", () => {
  const code = read(RENDERER);
  assert.match(code, /mem && mem\.tree/, "reads the folder tree");
  assert.match(code, /mem && mem\.root_md|mem\.root_md/, "reads 记忆.md");
  // Fresh every turn: the model must see its own writes in the same session.
  assert.match(code, /_fetchMemoryBlock\(\);/);
  assert.doesNotMatch(code, /const SKILLS_CONTEXT_TTL[\s\S]{0,200}memory[\s\S]{0,80}60s/,
    "memory must not fall under the 60s skills cache");
});

test("no settings string still advertises the retired identity-note layer", () => {
  require("../src/settings-i18n");
  const STRINGS = globalThis.ClawdSettingsI18n.STRINGS;
  const offenders = [];
  for (const [lang, block] of Object.entries(STRINGS)) {
    for (const [key, value] of Object.entries(block)) {
      if (typeof value !== "string") continue;
      if (/identity notes|身份笔记|身分筆記|정체성 노트|アイデンティティノート/.test(value)) {
        offenders.push(`${lang}.${key}: ${value.slice(0, 60)}`);
      }
    }
  }
  assert.deepEqual(offenders, [], "stale identity-note copy:\n  " + offenders.join("\n  "));
});

test("both shells carry the same memory contract — they must not drift", () => {
  // The Tauri shell keeps its own copy of the renderer. When 2ca7303 changed
  // /api/memory it kept reading mem.memory / mem.user, so its memory block
  // silently became empty and its prompt still told the model to call
  // add/replace/remove. Parity is asserted here so the next API change has to
  // land in both files in the same commit.
  const shells = {
    electron: RENDERER,
    tauri: path.join(__dirname, "..", "..", "deskpt-tauri", "ui", "pet-chat-renderer.js"),
  };
  for (const [name, file] of Object.entries(shells)) {
    const code = read(file);
    const body = code.split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => !l.startsWith("//") && !l.startsWith("*")).join("\n");
    assert.match(body, /mem\.tree/, `${name}: does not read the 记忆 tree`);
    assert.match(body, /mem\.root_md/, `${name}: does not read 记忆.md`);
    assert.doesNotMatch(body, /mem\.memory\b|mem\.user\b/,
      `${name}: still reads the retired mem.memory / mem.user fields`);
    const promptLines = code.split(/\r?\n/)
      .map((l) => l.trim())
      .filter((l) => /MEMORY\.md|USER\.md/.test(l) && !l.startsWith("//") && !l.startsWith("*"));
    assert.deepEqual(promptLines, [], `${name}: prompt text still names the retired files`);
    for (const action of ["list", "read", "write", "delete"]) {
      assert.ok(new RegExp(`\\b${action}\\b`).test(code), `${name}: prompt omits '${action}'`);
    }
  }
});
