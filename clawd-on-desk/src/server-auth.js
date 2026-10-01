"use strict";

// Local HTTP authorization for the hook server (127.0.0.1:23333-23337).
//
// Threat model this closes (audit V-2): the server used to answer
// POST /state and POST /permission from ANY caller, and a browser happily
// sends cross-origin POSTs to localhost — CORS only stops the page from
// READING the response, not from writing to us. So any webpage could push a
// fabricated agent state or pop a permission bubble with attacker-chosen
// text, and identity was "verified" by a response header / JSON field that
// any local process can copy.
//
// Three independent gates, cheapest first:
//   1. Host must be loopback — blocks DNS rebinding and any non-local vhost.
//   2. Origin/Referer, when present, must be a loopback http(s) page — a
//      browser always sends one on cross-origin POST, a Node client never does.
//   3. A shared secret in `x-clawd-token`, read from ~/.clawd/server-token.
//      Only processes already running as this user can read it, so this is
//      what actually stops the webpage path (a page cannot read the file).
//
// Residual risk, deliberately out of scope: a local process running as the
// same user can read the token file. That is the OS boundary, not ours.

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { SERVER_TOKEN_PATH } = require("../hooks/server-config");

const TOKEN_HEADER = "x-clawd-token";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

function defaultTokenPath() {
  // One path, owned by hooks/server-config.js, because the hook client is
  // the other reader — two definitions drifting apart would just break
  // permission bubbles silently.
  return SERVER_TOKEN_PATH;
}

function randomToken() {
  return crypto.randomBytes(24).toString("base64url");
}

// Constant-time compare, so a wrong guess never leaks how much matched.
function tokenMatches(provided, expected) {
  if (typeof provided !== "string" || typeof expected !== "string") return false;
  if (!provided.length || !expected.length) return false;
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

/**
 * Read the shared token, creating it on first use.
 * @param {{filePath?: string, fsImpl?: typeof fs, random?: () => string}} [options]
 */
function loadOrCreateToken(options = {}) {
  const filePath = options.filePath || defaultTokenPath();
  const fss = options.fsImpl || fs;
  try {
    const existing = String(fss.readFileSync(filePath, "utf8") || "").trim();
    if (existing) return existing;
  } catch {
    // Missing file or directory: fall through and create it.
  }
  const token = (options.random ? options.random() : randomToken());
  try {
    fss.mkdirSync(path.dirname(filePath), { recursive: true });
    fss.writeFileSync(filePath, token + "\n", { encoding: "utf8", mode: 0o600 });
  } catch {
    // Unwritable home is not a reason to refuse to boot; the caller then
    // sees "no token" and every request is rejected, which is the safe side.
  }
  return token;
}

function parseHost(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return null;
  const withoutScheme = raw.replace(/^[a-z]+:\/\//, "");
  const host = withoutScheme.split("/")[0];
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    return end < 0 ? null : host.slice(0, end + 1);
  }
  return host.split(":")[0];
}

function hostIsLoopback(hostHeader) {
  const host = parseHost(hostHeader);
  if (!host) return false;
  if (LOOPBACK_HOSTS.has(host)) return true;
  // Any 127.x.x.x address is loopback on every OS we ship to.
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

function originIsLoopback(originHeader) {
  const raw = String(originHeader || "").trim().toLowerCase();
  if (!raw || raw === "null") return false;
  if (!/^https?:\/\//.test(raw)) return false;
  return hostIsLoopback(raw.replace(/^[a-z]+:\/\//, ""));
}

/**
 * Decide whether an inbound request may reach the state / permission routes.
 * @param {{headers?: object, token?: string}} input
 * @returns {{ok: boolean, reason?: string}}
 */
function authorizeRequest(input = {}) {
  const raw = input.headers || {};
  // Node lowercases inbound header names, but callers (and tests) may not —
  // look up case-insensitively instead of silently reading `undefined`.
  const lowered = {};
  for (const key of Object.keys(raw)) lowered[String(key).toLowerCase()] = raw[key];
  const get = (name) => lowered[name];

  if (!hostIsLoopback(get("host"))) return { ok: false, reason: "host" };

  const origin = get("origin") || get("referer");
  if (origin && !originIsLoopback(origin)) return { ok: false, reason: "origin" };

  if (!input.token) return { ok: false, reason: "no-server-token" };
  if (!tokenMatches(get(TOKEN_HEADER), input.token)) return { ok: false, reason: "token" };

  return { ok: true };
}

module.exports = {
  TOKEN_HEADER,
  authorizeRequest,
  defaultTokenPath,
  hostIsLoopback,
  loadOrCreateToken,
  originIsLoopback,
  parseHost,
  tokenMatches,
};
