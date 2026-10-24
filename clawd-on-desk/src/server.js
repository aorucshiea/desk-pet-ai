// src/server.js — HTTP server + routes (/state, /permission, /health)
// Extracted from main.js L1337-1528

const http = require("http");
const {
  DEFAULT_SERVER_PORT,
  RUNTIME_CONFIG_PATH,
  clearRuntimeConfig,
  getPortCandidates,
  readRuntimePort,
  writeRuntimeConfig,
} = require("../hooks/server-config");
const {
  entriesContainCommandMarker,
  entriesContainHttpHookUrl,
  settingsNeedClaudeHookResync,
  createClaudeSettingsWatcher,
} = require("./claude-settings-watcher");
const { createIntegrationSyncRuntime } = require("./integration-sync");
const {
  sendStateHealthResponse,
  handleStatePost,
} = require("./server-route-state");
const {
  handlePermissionPost,
  shouldBypassCCBubble,
  shouldBypassCodexBubble,
  shouldBypassOpencodeBubble,
} = require("./server-route-permission");
const {
  getCodexOfficialTurnKey,
  resolveCodexOfficialHookState,
} = require("./server-codex-official-turns");
const {
  HOOK_EVENT_RING_SIZE_PER_AGENT,
  createSingleRequestHookEventRecorder,
  recordHookEventInBuffer,
  getRecentHookEventsFromBuffer,
} = require("./server-hook-events");
const {
  normalizePermissionSuggestions,
  normalizeElicitationToolInput,
  normalizeCodexPermissionToolInput,
  normalizeToolMatchValue,
  buildToolInputFingerprint,
  findPendingPermissionForStateEvent,
} = require("./server-permission-utils");
const {
  authorizeRequest,
  loadOrCreateToken,
} = require("./server-auth");

module.exports = function initServer(ctx) {

const createHttpServer = ctx.createHttpServer || http.createServer.bind(http);
const setImmediateFn = ctx.setImmediate || setImmediate;
const nowFn = typeof ctx.now === "function" ? ctx.now : Date.now;
const clearRuntimeConfigFn = ctx.clearRuntimeConfig || clearRuntimeConfig;
const getPortCandidatesFn = ctx.getPortCandidates || getPortCandidates;
const readRuntimePortFn = ctx.readRuntimePort || readRuntimePort;
const writeRuntimeConfigFn = ctx.writeRuntimeConfig || writeRuntimeConfig;
const CLAUDE_HOOK_GUARD_NOTICE_TTL_MS = 30 * 60 * 1000;

let httpServer = null;
// Shared secret for the hook server, created on first boot and read by the
// hook client from ~/.clawd/server-token (see hooks/server-config.js).
let serverToken = "";
let lastRejectionLogAt = 0;

function rejectUnauthorizedRequest(res, reason, req) {
  // One line per 10s, so a page that retries every second cannot fill the
  // log — but a real attack (or a broken hook) stays visible.
  const now = nowFn();
  if (!lastRejectionLogAt || now - lastRejectionLogAt > 10000) {
    lastRejectionLogAt = now;
    console.warn(`Clawd state server rejected ${req.method} ${req.url}: ${reason}`);
  }
  res.writeHead(401, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "unauthorized", reason }));
}
let activeServerPort = null;
let lastClaudeHookGuardNotice = null;
const codexOfficialTurns = new Map();
const recentHookEvents = new Map();

function shouldDropForDnd() {
  if (typeof ctx.shouldDropForDnd === "function") {
    try {
      return !!ctx.shouldDropForDnd();
    } catch {}
  }
  return !!ctx.doNotDisturb;
}

function recordHookEvent(data, route, outcome) {
  return recordHookEventInBuffer(recentHookEvents, data, route, outcome, { now: nowFn });
}

function createRequestHookRecorder(data, defaultRoute) {
  return createSingleRequestHookEventRecorder(recordHookEvent, data, defaultRoute);
}

function getRecentHookEvents(options = {}) {
  return getRecentHookEventsFromBuffer(recentHookEvents, options);
}

function clearRecentHookEvents(agentId) {
  if (typeof agentId === "string" && agentId) recentHookEvents.delete(agentId);
  else recentHookEvents.clear();
}

function shouldManageClaudeHooks() {
  return ctx.manageClaudeHooksAutomatically !== false;
}

function isAgentEnabled(agentId) {
  if (typeof ctx.isAgentEnabled !== "function") return true;
  return ctx.isAgentEnabled(agentId) !== false;
}

function shouldSyncAgentIntegration(agentId) {
  if (typeof ctx.shouldSyncAgentIntegration === "function") {
    return ctx.shouldSyncAgentIntegration(agentId) !== false;
  }
  return isAgentEnabled(agentId);
}

function getHookServerPort() {
  return activeServerPort || readRuntimePortFn() || DEFAULT_SERVER_PORT;
}

function getRuntimeStatus() {
  let address = null;
  try {
    address = httpServer && typeof httpServer.address === "function" ? httpServer.address() : null;
  } catch {
    address = null;
  }
  const addressPort = address && typeof address === "object" && Number.isInteger(address.port)
    ? address.port
    : null;
  const port = activeServerPort || addressPort || null;
  const runtimePort = readRuntimePortFn();
  return {
    listening: !!port && (!httpServer || httpServer.listening !== false),
    port,
    runtimePath: typeof ctx.runtimeConfigPath === "string" ? ctx.runtimeConfigPath : RUNTIME_CONFIG_PATH,
    runtimePort,
    runtimeFileExists: Number.isInteger(runtimePort),
    runtimeMatches: Number.isInteger(port) && runtimePort === port,
  };
}

function getClaudeHookGuardStatus() {
  if (!lastClaudeHookGuardNotice) return null;
  if (nowFn() - lastClaudeHookGuardNotice.at > CLAUDE_HOOK_GUARD_NOTICE_TTL_MS) return null;
  return { ...lastClaudeHookGuardNotice };
}

function clearClaudeHookGuardStatus() {
  const hadNotice = !!lastClaudeHookGuardNotice;
  lastClaudeHookGuardNotice = null;
  return hadNotice;
}

const integrationSync = createIntegrationSyncRuntime({
  ctx,
  getHookServerPort,
  shouldManageClaudeHooks,
  isAgentEnabled,
  shouldSyncAgentIntegration,
  startClaudeSettingsWatcher,
  stopClaudeSettingsWatcher,
});
const {
  syncClawdHooks,
  syncGeminiHooks,
  syncAntigravityHooks,
  syncCursorHooks,
  syncCodeBuddyHooks,
  syncKiroHooks,
  syncKimiHooks,
  syncCodexHooks,
  syncOpencodePlugin,
  syncPiExtension,
  syncIntegrationForAgent: syncIntegrationForAgentBase,
  repairIntegrationForAgent: repairIntegrationForAgentBase,
  stopIntegrationForAgent,
  uninstallIntegrationForAgent,
  syncEnabledStartupIntegrations,
} = integrationSync;

function notifySuspiciousShrink(before, after) {
  lastClaudeHookGuardNotice = {
    type: "suspicious-shrink",
    at: nowFn(),
    before: before ? { ...before } : null,
    after: after ? { ...after } : null,
  };
  if (typeof ctx.notifySuspiciousShrink === "function") {
    ctx.notifySuspiciousShrink(before, after, lastClaudeHookGuardNotice);
  }
}

function shouldClearClaudeHookGuardAfterSync(agentId, result) {
  if (agentId !== "claude-code") return false;
  if (result === false) return false;
  if (result && typeof result === "object" && result.status === "error") return false;
  return true;
}

function clearClaudeHookGuardAfterClaudeSync(agentId, result) {
  if (shouldClearClaudeHookGuardAfterSync(agentId, result)) clearClaudeHookGuardStatus();
  return result;
}

function syncIntegrationForAgent(agentId) {
  return clearClaudeHookGuardAfterClaudeSync(agentId, syncIntegrationForAgentBase(agentId));
}

function repairIntegrationForAgent(agentId, options = {}) {
  return clearClaudeHookGuardAfterClaudeSync(agentId, repairIntegrationForAgentBase(agentId, options));
}

function repairRuntimeStatus() {
  const status = getRuntimeStatus();
  if (status && status.listening && Number.isInteger(status.port)) {
    const written = writeRuntimeConfigFn(status.port);
    return written
      ? { status: "ok" }
      : { status: "error", message: "Failed to write runtime config" };
  }
  if (!httpServer) {
    startHttpServer();
    return { status: "ok" };
  }
  return {
    status: "error",
    message: "Local server is not listening; restart Clawd",
  };
}

const claudeSettingsWatcher = createClaudeSettingsWatcher({
  ...ctx,
  shouldManageClaudeHooks,
  isAgentEnabled,
  shouldSyncAgentIntegration,
  getHookServerPort,
  syncClawdHooks,
  notifySuspiciousShrink,
});

// Watch ~/.claude/ directory for settings.json overwrites (e.g. CC-Switch)
// that wipe our hooks. Re-register when hooks disappear.
// Watch the directory (not the file) because atomic rename replaces the inode
// and fs.watch on the old file silently stops firing on Windows.
function startClaudeSettingsWatcher() {
  return claudeSettingsWatcher.start();
}

function stopClaudeSettingsWatcher() {
  return claudeSettingsWatcher.stop();
}

function handleWorldOpPost(req, res, { ctx }) {
  const chunks = [];
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > 8192) {
      try { req.destroy(); } catch {}
      return;
    }
    chunks.push(chunk);
  });
  req.on("end", () => {
    let payload = {};
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {}
    const geo = typeof ctx.getWorldGeometry === "function" ? ctx.getWorldGeometry() : null;
    const reply = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    if (!geo || typeof geo.operateBody !== "function") {
      reply(503, { status: "error", message: "desktop physics is not running" });
      return;
    }
    const id = Number(payload && payload.id);
    const action = payload && typeof payload.action === "string" ? payload.action : "";
    let out;
    try {
      out = geo.operateBody(id, action, { distance: payload && payload.distance });
    } catch (err) {
      out = { status: "error", message: (err && err.message) || String(err) };
    }
    reply(out && out.status === "ok" ? 200 : 400, out);
  });
}

function handleWorldAddPost(req, res, { ctx }) {
  const chunks = [];
  req.on("data", (chunk) => chunks.push(chunk));
  req.on("end", () => {
    let payload = {};
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {}
    const reply = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    const geo = typeof ctx.getWorldGeometry === "function" ? ctx.getWorldGeometry() : null;
    if (!geo || typeof geo.addBodyFromHwnd !== "function") {
      reply(503, { status: "error", message: "world geometry unavailable" });
      return;
    }
    let out;
    try {
      out = geo.addBodyFromHwnd(payload && payload.hwnd);
    } catch (err) {
      out = { status: "error", message: (err && err.message) || String(err) };
    }
    reply(out.status === "ok" ? 200 : 400, out);
  });
}

function handlePetWalkPost(req, res, { ctx }) {
  const chunks = [];
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > 4096) {
      try { req.destroy(); } catch {}
      return;
    }
    chunks.push(chunk);
  });
  req.on("end", () => {
    let payload = {};
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {}
    const reply = (code, obj) => {
      res.writeHead(code, { "Content-Type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    const move = typeof ctx.walkPet === "function" ? ctx.walkPet : null;
    if (!move) {
      reply(503, { status: "error", message: "walk unavailable" });
      return;
    }
    let out;
    try {
      out = move(Number(payload && payload.dx) || 0, Number(payload && payload.dy) || 0);
      if (!out || typeof out !== "object") out = { status: "error", message: "walk returned nothing" };
    } catch (err) {
      out = { status: "error", message: (err && err.message) || String(err) };
    }
    reply(out.status === "ok" ? 200 : 400, out);
  });
}

function startHttpServer() {
  const loadTokenFn = ctx.loadServerToken || loadOrCreateToken;
  serverToken = loadTokenFn();
  if (!serverToken) {
    // Fail closed, but say so: without a persisted secret every hook request
    // is rejected below, which silently kills permission bubbles.
    console.error("Clawd state server could not create ~/.clawd/server-token — "
      + "state sync and permission bubbles will be rejected. Check that the home "
      + "directory is writable.");
  }
  httpServer = createHttpServer((req, res) => {
    // Everything on this socket is agent-state or a permission decision, so
    // nothing is served before the request proves it is a local, authorized
    // caller (audit V-2).
    const verdict = authorizeRequest({ headers: req.headers, token: serverToken });
    if (!verdict.ok) {
      rejectUnauthorizedRequest(res, verdict.reason, req);
      return;
    }
    if (req.method === "GET" && req.url === "/state") {
      sendStateHealthResponse(res, { getHookServerPort });
    } else if (req.method === "POST" && req.url === "/state") {
      handleStatePost(req, res, {
        ctx,
        createRequestHookRecorder,
        shouldDropForDnd,
        codexOfficialTurns,
      });
    } else if (req.method === "POST" && req.url === "/permission") {
      handlePermissionPost(req, res, {
        ctx,
        createRequestHookRecorder,
      });
    } else if (req.method === "GET" && req.url === "/world/bodies") {
      // 桌面刚体列表（AI 工具 pet_world_bodies 用）
      const geo = typeof ctx.getWorldGeometry === "function" ? ctx.getWorldGeometry() : null;
      const out = geo && typeof geo.listBodies === "function"
        ? geo.listBodies()
        : { status: "error", message: "world geometry unavailable", bodies: [] };
      // The gateway's decision loop ranks bodies by distance/bearing to the
      // pet; include the screen rect (DIP) and facing so it does not guess.
      if (out && out.status === "ok") {
        try {
          if (typeof ctx.getPetBounds === "function") {
            const pet = ctx.getPetBounds();
            if (pet) out.pet = pet;
          }
          if (typeof ctx.getPetFacing === "function") out.facing = ctx.getPetFacing();
          if (typeof ctx.getWorkArea === "function") out.wa = ctx.getWorkArea();
        } catch {}
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(out));
    } else if (req.method === "GET" && req.url === "/world/windows") {
      // AI 自主收养刚体第一步：从自己的视角看桌面上的候选窗户
      const geo = typeof ctx.getWorldGeometry === "function" ? ctx.getWorldGeometry() : null;
      const wout = geo && typeof geo.listWindows === "function"
        ? geo.listWindows()
        : { status: "error", message: "world geometry unavailable", windows: [] };
      if (wout && wout.status === "ok") {
        try {
          if (typeof ctx.getPetBounds === "function") {
            const pet = ctx.getPetBounds();
            if (pet) wout.pet = pet;
          }
          if (typeof ctx.getPetFacing === "function") wout.facing = ctx.getPetFacing();
          if (typeof ctx.getWorkArea === "function") wout.wa = ctx.getWorkArea();
        } catch {}
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(wout));
    } else if (req.method === "POST" && req.url === "/world/add") {
      // AI 自主收养刚体第二步：{ hwnd } → 注册成刚体（宠物自己选的窗户）
      handleWorldAddPost(req, res, { ctx });
    } else if (req.method === "POST" && req.url === "/world/op") {
      // AI 操作刚体：{ id, action: "push"|"pull"|"kick"|"grab", distance? }
      handleWorldOpPost(req, res, { ctx });
    } else if (req.method === "POST" && req.url === "/pet/walk") {
      // AI 自主移动：{ dx, dy }（决策层/测试用，clamp 到工作区）
      handlePetWalkPost(req, res, { ctx });
    } else {
      res.writeHead(404);
      res.end();
    }
  });

  const listenPorts = getPortCandidatesFn();
  let listenIndex = 0;
  httpServer.on("error", (err) => {
    if (!activeServerPort && err.code === "EADDRINUSE" && listenIndex < listenPorts.length - 1) {
      listenIndex++;
      httpServer.listen(listenPorts[listenIndex], "127.0.0.1");
      return;
    }
    if (!activeServerPort && err.code === "EADDRINUSE") {
      const firstPort = listenPorts[0];
      const lastPort = listenPorts[listenPorts.length - 1];
      console.warn(`Ports ${firstPort}-${lastPort} are occupied — state sync and permission bubbles are disabled`);
    } else {
      console.error("HTTP server error:", err.message);
    }
  });

  httpServer.on("listening", () => {
    activeServerPort = listenPorts[listenIndex];
    writeRuntimeConfigFn(activeServerPort);
    console.log(`Clawd state server listening on 127.0.0.1:${activeServerPort}`);
    // Defer hook/plugin registration off the startup path. Each sync call
    // reads+parses+writes a config JSON (50-150ms cumulative on slow disks),
    // and they operate on independent files for independent agents, so
    // none of them need to block the HTTP server from accepting traffic.
    setImmediateFn(() => {
      syncEnabledStartupIntegrations();
    });
  });

  httpServer.listen(listenPorts[listenIndex], "127.0.0.1");
}

function cleanup() {
  clearRuntimeConfigFn();
  clearClaudeHookGuardStatus();
  stopClaudeSettingsWatcher();
  if (httpServer) httpServer.close();
}

return {
  startHttpServer,
  getHookServerPort,
  getRuntimeStatus,
  getClaudeHookGuardStatus,
  clearClaudeHookGuardStatus,
  getRecentHookEvents,
  clearRecentHookEvents,
  syncClawdHooks,
  syncGeminiHooks,
  syncAntigravityHooks,
  syncCursorHooks,
  syncCodeBuddyHooks,
  syncKiroHooks,
  syncKimiHooks,
  syncCodexHooks,
  syncOpencodePlugin,
  syncPiExtension,
  syncIntegrationForAgent,
  repairIntegrationForAgent,
  uninstallIntegrationForAgent,
  repairRuntimeStatus,
  stopIntegrationForAgent,
  startClaudeSettingsWatcher,
  stopClaudeSettingsWatcher,
  cleanup,
};

};

module.exports.__test = {
  entriesContainCommandMarker,
  entriesContainHttpHookUrl,
  settingsNeedClaudeHookResync,
  shouldBypassCCBubble,
  shouldBypassCodexBubble,
  shouldBypassOpencodeBubble,
  normalizePermissionSuggestions,
  normalizeElicitationToolInput,
  normalizeCodexPermissionToolInput,
  normalizeToolMatchValue,
  buildToolInputFingerprint,
  findPendingPermissionForStateEvent,
  getCodexOfficialTurnKey,
  resolveCodexOfficialHookState,
  recordHookEventInBuffer,
  getRecentHookEventsFromBuffer,
  createSingleRequestHookEventRecorder,
  HOOK_EVENT_RING_SIZE_PER_AGENT,
};
