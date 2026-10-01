"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "src", "settings-tab-pet.js");
const I18N_SRC = path.join(__dirname, "..", "src", "settings-i18n.js");

// 机长: "推理后端只能选 cpu，无法选 vulkan 和 cuda".
// Two separate lies produced that: the sidecar's Windows branch never put
// cuda into `available`, and this renderer looped over a literal
// ["cpu","vulkan"] — so even a correct payload could not reach CUDA. The
// sidecar now reports what the installed engine can address (plus a reason
// for what it cannot), and the row renders whatever it is told.

test("推理后端 renders the backends the sidecar reports, not a literal list", () => {
  const code = fs.readFileSync(SRC, "utf8");
  assert.doesNotMatch(code, /for \(const device of \["cpu", "vulkan"\]\)/,
    "hardcoding the option list is what made CUDA unpickable");
  assert.match(code, /const pickable = \["cpu", "cuda", "vulkan", "metal"\]\.filter\(\(d\) => available\.includes\(d\)\)/);
  // Unavailable-but-installed backends stay visible and carry the reason.
  assert.match(code, /const blocked = \["cuda", "vulkan"\]\.filter/);
  assert.match(code, /title: String\(reasons\[device\] \|\| ""\)/);
  assert.match(code, /disabled: true/);
  // A selectable backend explains itself too (Vulkan is experimental).
  assert.match(code, /device === "vulkan" \? t\("petBackendVulkanExperimental"\) : reasons\[device\]/);
});

test("CUDA has a label in every product language", () => {
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(I18N_SRC, "utf8"), sandbox, { filename: "settings-i18n.js" });
  const STRINGS = sandbox.ClawdSettingsI18n.STRINGS;
  const code = fs.readFileSync(SRC, "utf8");
  assert.match(code, /if \(device === "cuda"\) return t\("petBackendCuda"\)/);
  for (const lang of ["en", "zh", "zh-TW", "ko", "ja"]) {
    const block = STRINGS[lang] || {};
    assert.equal(block.petBackendCuda, "CUDA", `${lang} lost the CUDA label`);
    assert.ok(typeof block.petRowBackendDesc === "string" && block.petRowBackendDesc.length > 0,
      `${lang} lost the backend description`);
  }
  // The description must not promise CPU-only behaviour any more.
  assert.doesNotMatch(STRINGS.zh.petRowBackendDesc, /默认使用更稳定的 CPU/);
});
