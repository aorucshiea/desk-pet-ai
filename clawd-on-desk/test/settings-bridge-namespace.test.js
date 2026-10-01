"use strict";

// The settings renderer talks to main through contextBridge namespaces
// (settingsAPI / petSettings / doctor / remoteSsh). Reading a method off the
// WRONG namespace is silent: `typeof undefined !== "function"`, so every tab
// guard fails closed and the UI reports "unavailable" while the gateway is
// perfectly healthy. That is exactly how the 自我进化 tab shipped dead —
// pluginsState & friends live in petSettings, the tab asked settingsAPI.
// This test pins every literal `window.<ns>.<method>` reference in the tabs
// against the surface preload-settings.js actually exposes.

const { describe, it } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const SRC = path.join(__dirname, "..", "src");

function parseBridges() {
  const src = fs.readFileSync(path.join(SRC, "preload-settings.js"), "utf8");
  const bridges = {};
  const open = /contextBridge\.exposeInMainWorld\("(\w+)",\s*\{/g;
  let m;
  while ((m = open.exec(src))) {
    const name = m[1];
    const body = src.slice(m.index + m[0].length);
    const end = body.indexOf("\n});");
    const block = end === -1 ? body : body.slice(0, end);
    const keys = new Set();
    for (const line of block.split(/\r?\n/)) {
      const k = /^  ([A-Za-z_$][\w$]*)\s*[:(]/.exec(line);
      if (k) keys.add(k[1]);
    }
    bridges[name] = keys;
  }
  return bridges;
}

function tabFiles() {
  return fs.readdirSync(SRC)
    .filter((f) => /^settings-tab-.*\.js$/.test(f))
    .sort();
}

describe("settings bridge namespace contract", () => {
  const bridges = parseBridges();

  it("parses the four exposed namespaces from preload-settings.js", () => {
    for (const ns of ["settingsAPI", "petSettings", "doctor", "remoteSsh"]) {
      assert.ok(bridges[ns] && bridges[ns].size > 0, `${ns} should expose methods`);
    }
  });

  for (const file of tabFiles()) {
    it(`${file} only calls methods on the namespace that exposes them`, () => {
      const src = fs.readFileSync(path.join(SRC, file), "utf8");
      const re = /window\.(settingsAPI|petSettings|doctor|remoteSsh)\.([A-Za-z_$][\w$]*)/g;
      const problems = [];
      let m;
      while ((m = re.exec(src))) {
        const [, ns, method] = m;
        if (!bridges[ns].has(method)) {
          const owner = Object.keys(bridges).find((k) => bridges[k].has(method));
          problems.push(`window.${ns}.${method}${owner ? ` (lives in ${owner})` : " (exists nowhere)"}`);
        }
      }
      assert.deepStrictEqual([...new Set(problems)], []);
    });
  }
});
