"use strict";

// Audit V-2: the hook server on 127.0.0.1:23333-23337 used to answer
// GET/POST /state and POST /permission from ANY caller. A browser can POST to
// localhost cross-origin (CORS only blocks READING the answer), so any webpage
// could push a fabricated agent state or pop a permission bubble with
// attacker-chosen text. These tests drive the real request handler.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const EventEmitter = require("node:events");

const initServer = require("../src/server");

const TEST_TOKEN = "gate-test-token-0123456789";

function makeFakeHttp() {
  let capturedHandler = null;
  function createHttpServer(handler) {
    capturedHandler = handler;
    const server = new EventEmitter();
    server.listen = function () { this.emit("listening"); };
    server.close = function () {};
    return server;
  }
  return { createHttpServer, getHandler: () => capturedHandler };
}

function start() {
  const http = makeFakeHttp();
  const ctx = {
    loadServerToken: () => TEST_TOKEN,
    createHttpServer: http.createHttpServer,
    setImmediate: () => {},
    getPortCandidates: () => [23333],
    writeRuntimeConfig: () => true,
    clearRuntimeConfig: () => true,
    readRuntimePort: () => null,
    permLog: () => {},
    updateLog: () => {},
  };
  const api = initServer(ctx);
  api.startHttpServer();
  return http.getHandler();
}

function call(handler, headers, method = "GET", url = "/state") {
  return new Promise((resolve) => {
    const req = new EventEmitter();
    req.method = method;
    req.url = url;
    req.headers = headers;
    const res = new EventEmitter();
    res.statusCode = null;
    res.body = "";
    res.writeHead = (code) => { res.statusCode = code; };
    res.end = (chunk) => {
      if (chunk) res.body += chunk;
      resolve({ statusCode: res.statusCode, body: res.body });
    };
    setImmediate(() => { req.emit("end"); });
    handler(req, res);
  });
}

const LOCAL = { host: "127.0.0.1:23333" };

test("GET /state answers an authorized local client", async () => {
  const handler = start();
  const res = await call(handler, { ...LOCAL, "x-clawd-token": TEST_TOKEN });
  assert.equal(res.statusCode, 200);
  assert.match(res.body, /"app":"clawd-on-desk"/);
});

test("a webpage-style POST without the secret is refused", async () => {
  const handler = start();
  const res = await call(handler, {
    ...LOCAL,
    origin: "https://evil.example",
    "content-type": "text/plain",
  }, "POST", "/permission");
  assert.equal(res.statusCode, 401);
  assert.match(res.body, /"reason":"origin"/);
});

test("no token header is refused even from loopback", async () => {
  const handler = start();
  const res = await call(handler, { ...LOCAL }, "POST", "/permission");
  assert.equal(res.statusCode, 401);
  assert.match(res.body, /"reason":"token"/);
});

test("a wrong token is refused", async () => {
  const handler = start();
  const res = await call(handler, {
    ...LOCAL, "x-clawd-token": "guessed-guessed-guessed",
  }, "POST", "/permission");
  assert.equal(res.statusCode, 401);
});

test("a non-loopback Host is refused before any other check", async () => {
  const handler = start();
  const res = await call(handler, {
    host: "rebound.attacker.test:23333", "x-clawd-token": TEST_TOKEN,
  });
  assert.equal(res.statusCode, 401);
  assert.match(res.body, /"reason":"host"/);
});

test("an authorized client still reaches the 404 path, not the gate", async () => {
  const handler = start();
  const res = await call(handler, { ...LOCAL, "x-clawd-token": TEST_TOKEN }, "GET", "/nope");
  assert.equal(res.statusCode, 404);
});
