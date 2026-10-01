"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const jsonUtils = require("../hooks/json-utils");

// Windows routinely holds a just-written file for a few tens of ms
// (Defender, the search indexer), and renaming onto an open destination
// then fails with EPERM. That is what made the hook-installer suite pick a
// DIFFERENT failing test on every run:
//   Error: EPERM: operation not permitted,
//     rename 'C:\...\Temp\clawd-cursor-xxxx\.hooks.json.45364.1790832192077.tmp'
// The write succeeded; only the swap was momentarily refused, so the shared
// atomic writer retries instead of losing the registration.

function codeError(code) {
  const err = new Error(`${code}: simulated`);
  err.code = code;
  return err;
}

function withFakeRename(behaviour, fn) {
  const real = fs.renameSync;
  let calls = 0;
  fs.renameSync = function (from, to) {
    calls += 1;
    const outcome = behaviour(calls);
    if (outcome instanceof Error) throw outcome;
    return real(from, to);
  };
  try {
    return { result: fn(), calls, restore: () => { fs.renameSync = real; } };
  } catch (err) {
    fs.renameSync = real;
    throw err;
  } finally {
    fs.renameSync = real;
  }
}

function tempTarget() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clawd-rename-"));
  return { dir, file: path.join(dir, "hooks.json") };
}

test("renameWithRetrySync rides out a transient EPERM", () => {
  const { dir, file } = tempTarget();
  try {
    const staged = path.join(dir, ".staged.tmp");
    fs.writeFileSync(staged, "{\"ok\":true}", "utf8");
    const run = withFakeRename((n) => (n <= 2 ? codeError("EPERM") : undefined),
      () => jsonUtils.renameWithRetrySync(staged, file));
    assert.ok(run.calls >= 3, `expected retries, saw ${run.calls} attempts`);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), { ok: true });
    assert.equal(fs.existsSync(staged), false, "the staged file was swapped, not copied");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("renameWithRetrySync still reports a real failure once, immediately", () => {
  const { dir, file } = tempTarget();
  try {
    const staged = path.join(dir, ".staged.tmp");
    fs.writeFileSync(staged, "x", "utf8");
    assert.throws(
      () => withFakeRename(() => codeError("ENOENT"), () => jsonUtils.renameWithRetrySync(staged, file)),
      /ENOENT/,
    );
    assert.ok(fs.existsSync(staged), "nothing was renamed");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("renameWithRetrySync gives up after its budget instead of hanging", () => {
  const { dir, file } = tempTarget();
  try {
    const staged = path.join(dir, ".staged.tmp");
    fs.writeFileSync(staged, "x", "utf8");
    let attempts = 0;
    const real = fs.renameSync;
    fs.renameSync = () => { attempts += 1; throw codeError("EPERM"); };
    try {
      assert.throws(() => jsonUtils.renameWithRetrySync(staged, file), /EPERM/);
    } finally {
      fs.renameSync = real;
    }
    assert.equal(attempts, 5, `expected 5 attempts, saw ${attempts}`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("writeJsonAtomic lands the config through the retrying swap", () => {
  const { dir, file } = tempTarget();
  try {
    const run = withFakeRename((n) => (n === 1 ? codeError("EBUSY") : undefined),
      () => jsonUtils.writeJsonAtomic(file, { version: 1 }));
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), { version: 1 });
    assert.ok(run.calls >= 2);
    // No stray tmp files left behind in the config directory.
    assert.deepEqual(fs.readdirSync(dir), ["hooks.json"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("the async writer retries too", async () => {
  const { dir, file } = tempTarget();
  const staged = path.join(dir, ".staged.tmp");
  try {
    fs.writeFileSync(staged, "{\"a\":1}", "utf8");
    const real = fs.promises.rename;
    let calls = 0;
    fs.promises.rename = async (from, to) => {
      calls += 1;
      if (calls === 1) throw codeError("EACCES");
      return real(from, to);
    };
    try {
      await jsonUtils.renameWithRetryAsync(staged, file);
    } finally {
      fs.promises.rename = real;
    }
    assert.equal(calls, 2);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), { a: 1 });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
