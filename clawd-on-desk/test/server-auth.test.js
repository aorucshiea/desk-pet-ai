"use strict";

const { test, describe } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const auth = require("../src/server-auth");

// Audit V-2: the hook server answered POST /state and POST /permission from
// anyone on the machine, and a browser CAN cross-origin POST to localhost —
// CORS only stops the page reading the answer. So any webpage could push a
// fake agent state or pop a permission bubble with attacker-chosen text, and
// "is this really Clawd?" was decided by a header any process can copy.

describe("hostIsLoopback", () => {
  test("accepts every loopback spelling we bind", () => {
    for (const host of ["127.0.0.1:23333", "localhost:23333", "[::1]:23333", "127.0.0.5"]) {
      assert.equal(auth.hostIsLoopback(host), true, host);
    }
  });

  test("rejects rebinding and non-local hosts", () => {
    for (const host of ["attacker.test:23333", "evil.com", "10.0.0.8:23333", "", undefined]) {
      assert.equal(auth.hostIsLoopback(host), false, String(host));
    }
  });
});

describe("originIsLoopback", () => {
  test("a local web panel page is fine, an internet page is not", () => {
    assert.equal(auth.originIsLoopback("http://127.0.0.1:18999"), true);
    assert.equal(auth.originIsLoopback("http://localhost:18999"), true);
    assert.equal(auth.originIsLoopback("https://evil.example"), false);
    // Sandboxed iframes and file:// pages report exactly "null".
    assert.equal(auth.originIsLoopback("null"), false);
    assert.equal(auth.originIsLoopback(""), false);
  });
});

describe("tokenMatches", () => {
  test("only an identical token passes", () => {
    assert.equal(auth.tokenMatches("abc123", "abc123"), true);
    assert.equal(auth.tokenMatches("abc12", "abc123"), false);
    assert.equal(auth.tokenMatches("abc1234", "abc123"), false);
    assert.equal(auth.tokenMatches("", "abc123"), false);
    assert.equal(auth.tokenMatches(undefined, "abc123"), false);
    assert.equal(auth.tokenMatches("abc123", ""), false);
  });
});

describe("authorizeRequest", () => {
  const TOKEN = "s3cret-token-value";

  test("the hook client path: Node request, no Origin, token present", () => {
    const verdict = auth.authorizeRequest({
      headers: { host: "127.0.0.1:23333", "x-clawd-token": TOKEN },
      token: TOKEN,
    });
    assert.deepEqual(verdict, { ok: true });
  });

  test("the webpage path is closed: cross-origin POST without a token", () => {
    // This is the exact request the audit describes:
    // fetch("http://127.0.0.1:23333/permission", {method:"POST", mode:"no-cors"})
    const verdict = auth.authorizeRequest({
      headers: {
        host: "127.0.0.1:23333",
        origin: "https://evil.example",
        "content-type": "text/plain",
      },
      token: TOKEN,
    });
    assert.equal(verdict.ok, false);
    assert.equal(verdict.reason, "origin");
  });

  test("a same-origin page still needs the secret", () => {
    const verdict = auth.authorizeRequest({
      headers: { host: "127.0.0.1:23333", origin: "http://127.0.0.1:18999" },
      token: TOKEN,
    });
    assert.equal(verdict.reason, "token");
  });

  test("a wrong token is rejected even when Origin looks local", () => {
    const verdict = auth.authorizeRequest({
      headers: {
        host: "127.0.0.1:23333",
        origin: "http://localhost:18999",
        "x-clawd-token": "guessed",
      },
      token: TOKEN,
    });
    assert.equal(verdict.reason, "token");
  });

  test("DNS rebinding (Host points elsewhere) is rejected first", () => {
    const verdict = auth.authorizeRequest({
      headers: { host: "rebound.attacker.test:23333", "x-clawd-token": TOKEN },
      token: TOKEN,
    });
    assert.equal(verdict.reason, "host");
  });

  test("if the server has no token at all, nothing is authorized", () => {
    const verdict = auth.authorizeRequest({
      headers: { host: "127.0.0.1:23333", "x-clawd-token": "" },
      token: "",
    });
    assert.equal(verdict.reason, "no-server-token");
  });

  test("header lookup works regardless of casing", () => {
    const verdict = auth.authorizeRequest({
      headers: { Host: "127.0.0.1:23333", "X-Clawd-Token": TOKEN },
      token: TOKEN,
    });
    assert.equal(verdict.ok, true);
  });
});

describe("loadOrCreateToken", () => {
  test("creates a 0600 file once and reuses it", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clawd-token-"));
    const file = path.join(dir, "server-token");
    try {
      const first = auth.loadOrCreateToken({ filePath: file });
      assert.ok(first.length >= 32, "token should be long enough to guess-free");
      assert.ok(fs.existsSync(file));
      const mode = fs.statSync(file).mode & 0o777;
      // Windows ignores the mode bits; POSIX must not expose the secret.
      if (process.platform !== "win32") assert.equal(mode, 0o600);
      const second = auth.loadOrCreateToken({ filePath: file });
      assert.equal(second, first, "the token must be stable across restarts");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  test("an unwritable path yields no token, which fails closed", () => {
    const verdict = auth.authorizeRequest({
      headers: { host: "127.0.0.1:23333", "x-clawd-token": "whatever" },
      token: auth.loadOrCreateToken({
        filePath: path.join(os.tmpdir(), "clawd-missing-dir-xyz", "server-token"),
        fsImpl: { readFileSync: () => { throw new Error("ENOENT"); }, mkdirSync: () => { throw new Error("EACCES"); }, writeFileSync: () => { throw new Error("EACCES"); } },
        random: () => "",
      }),
    });
    assert.equal(verdict.ok, false);
  });
});
