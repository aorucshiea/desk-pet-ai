"use strict";

const defaultFs = require("fs");
const defaultPath = require("path");
const defaultHttp = require("http");
const { detectAgentInstallations: defaultDetectAgentInstallations } = require("./agent-installation-detector");
const settingsThemeImporter = require("./settings-theme-importer");
const productMetadata = require("./product-metadata");
const { DEFAULT_THEME_ID } = require("./default-theme");

// Sidecar gateway lives on localhost (same constants as minicpm-chat.js:
// MINICPM_PORT override, else 18765). The settings window uses these to
// expose the llama.cpp engine self-update (check + apply) without owning
// the sidecar lifecycle.
const SIDECAR_HOST = "127.0.0.1";
const SIDECAR_PORT = Number(process.env.MINICPM_PORT) || 18765;

// B-5: gateway requires X-MiniCPM-Token on every API call. Read + cache.
let _gatewayTokenCache = null;
function gatewayToken() {
  if (_gatewayTokenCache) return _gatewayTokenCache;
  try {
    const p = defaultPath.join(require("os").homedir(), ".minicpm", "gateway-token");
    _gatewayTokenCache = defaultFs.readFileSync(p, "utf8").trim();
  } catch {
    _gatewayTokenCache = "";
  }
  return _gatewayTokenCache;
}

function sidecarJson(method, pathname, timeoutMs = 3000, body = null) {
  return new Promise((resolve) => {
    const payload = body ? JSON.stringify(body) : null;
    const headers = { "x-minicpm-token": gatewayToken() };
    if (payload) {
      headers["content-type"] = "application/json";
      headers["content-length"] = Buffer.byteLength(payload);
    }
    const req = defaultHttp.request(
      {
        hostname: SIDECAR_HOST, port: SIDECAR_PORT, path: pathname, method, timeout: timeoutMs,
        headers,
      },
      (res) => {
        let buf = "";
        res.setEncoding("utf8");
        res.on("data", (c) => { buf += c; });
        res.on("end", () => {
          try { resolve({ ok: true, json: JSON.parse(buf) }); }
          catch { resolve({ ok: true, json: null }); }
        });
      }
    );
    req.on("error", (err) => resolve({ ok: false, error: err.message }));
    req.on("timeout", () => { req.destroy(); resolve({ ok: false, error: "timeout" }); });
    if (payload) req.write(payload);
    req.end();
  });
}

function platformTriple() {
  const arch = process.arch === "arm64" ? "arm64" : process.arch === "x64" ? "x64" : process.arch;
  if (process.platform === "darwin") return "mac-" + arch;
  if (process.platform === "win32") return "win-" + arch;
  if (process.platform === "linux") return "linux-" + arch;
  return process.platform + "-" + arch;
}

// Locate the installed llama-server binary WITHOUT the sidecar running —
// the local engine version must be visible even when the gateway is
// down. Priority: packaged sidecar-bin → dev bin/<triple>/ → backend
// subdir (vulkan/cuda).
function locateLlamaServer() {
  const ext = process.platform === "win32" ? ".exe" : "";
  const name = "llama-server" + ext;
  const triple = platformTriple();
  const candidates = [];
  try {
    if (process.resourcesPath) {
      candidates.push(defaultPath.join(process.resourcesPath, "sidecar-bin", name));
      candidates.push(defaultPath.join(process.resourcesPath, "sidecar-bin", triple, name));
    }
  } catch {}
  const devRoot = defaultPath.join(__dirname, "..", "..", "minicpm-sidecar", "bin", triple);
  candidates.push(defaultPath.join(devRoot, name));
  for (const backend of ["vulkan", "cuda", "metal"]) {
    candidates.push(defaultPath.join(devRoot, "backends", backend, name));
  }
  for (const c of candidates) {
    try { if (defaultFs.statSync(c).isFile()) return c; } catch {}
  }
  return null;
}

function sidecarEngineApply(timeoutMs = 0, onPhase = null, opts = null) {
  // POST /api/engine-update-apply[(-dir)] streams SSE phases (start/
  // transfer/swap/verify/complete/error/reloaded). Collect them all AND
  // forward each one through onPhase so the settings UI can paint a live
  // progress bar instead of waiting blind. opts = { path, body } for the
  // offline apply-from-dir variant.
  return new Promise((resolve) => {
    const pathname = (opts && opts.path) || "/api/engine-update-apply";
    const body = (opts && opts.body) ? JSON.stringify(opts.body) : null;
    const req = defaultHttp.request(
      {
        hostname: SIDECAR_HOST, port: SIDECAR_PORT, path: pathname,
        method: "POST", timeout: timeoutMs,
        headers: body
          ? { "content-type": "application/json", "content-length": Buffer.byteLength(body) }
          : { "content-type": "application/json", "content-length": 0 },
      },
      (res) => {
        let buf = "";
        const phases = [];
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          buf += chunk;
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            if (!block.startsWith("data:")) continue;
            try {
              const ev = JSON.parse(block.slice(5).trim());
              phases.push(ev);
              if (typeof onPhase === "function") {
                try { onPhase(ev); } catch {}
              }
            } catch {}
          }
        });
        res.on("end", () => {
          const last = phases[phases.length - 1] || {};
          resolve({ ok: true, phases, error: last.message || null });
        });
      }
    );
    req.on("error", (err) => resolve({ ok: false, error: err.message }));
    if (body) req.write(body);
    req.end();
  });
}

const SOUND_OVERRIDE_ASSET_EXTS = new Set([".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"]);
const SOUND_OVERRIDE_DIALOG_STRINGS = {
  en: { title: "Choose a sound file", filterName: "Audio" },
  zh: { title: "选择音效文件", filterName: "音频" },
  "zh-TW": { title: "選擇音效檔案", filterName: "音效" },
  ko: { title: "음향 파일 선택", filterName: "오디오" },
  ja: { title: "音声ファイルを選択", filterName: "音声" },
};

const REMOVE_THEME_DIALOG_STRINGS = {
  en: {
    delete: "Delete",
    cancel: "Cancel",
    message: (name) => `Delete theme "${name}"?`,
    detail: "This cannot be undone. All files for this theme will be removed from disk.",
  },
  zh: {
    delete: "删除",
    cancel: "取消",
    message: (name) => `确认删除主题 "${name}"？`,
    detail: "此操作不可撤销。主题的所有文件将从磁盘移除。",
  },
  "zh-TW": {
    delete: "刪除",
    cancel: "取消",
    message: (name) => `確定要刪除主題「${name}」？`,
    detail: "此動作無法復原。此主題的所有檔案都會從磁碟移除。",
  },
  ko: {
    delete: "삭제",
    cancel: "취소",
    message: (name) => `테마 "${name}"을(를) 삭제할까요?`,
    detail: "이 작업은 되돌릴 수 없습니다. 이 테마의 모든 파일이 디스크에서 제거됩니다.",
  },
  ja: {
    delete: "削除",
    cancel: "キャンセル",
    message: (name) => `テーマ "${name}" を削除しますか？`,
    detail: "この操作は元に戻せません。このテーマのすべてのファイルがディスクから削除されます。",
  },
};

function requiredDependency(value, name) {
  if (!value) throw new Error(`registerSettingsIpc requires ${name}`);
  return value;
}

function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function getSettingsDialogParent(event, { BrowserWindow, getSettingsWindow }) {
  const sender = event && event.sender;
  const fromSender = sender && BrowserWindow && typeof BrowserWindow.fromWebContents === "function"
    ? BrowserWindow.fromWebContents(sender)
    : null;
  return fromSender || (typeof getSettingsWindow === "function" ? getSettingsWindow() : null) || null;
}

function cleanupSiblingSoundOverrides(fs, path, overridesDir, soundName, keepExt) {
  let entries;
  try { entries = fs.readdirSync(overridesDir); }
  catch { return; }
  for (const entry of entries) {
    if (path.parse(entry).name !== soundName) continue;
    if (path.extname(entry).toLowerCase() === keepExt) continue;
    try { fs.unlinkSync(path.join(overridesDir, entry)); } catch {}
  }
}

function rememberRuntimeSoundOverrideFile({ getActiveTheme }, themeId, soundName, absPath) {
  const activeTheme = getActiveTheme();
  if (!activeTheme || activeTheme._id !== themeId) return;
  if (typeof soundName !== "string" || !soundName) return;
  if (typeof absPath !== "string" || !absPath) return;
  const nextOverrideMap = isPlainObject(activeTheme._soundOverrideFiles)
    ? { ...activeTheme._soundOverrideFiles }
    : {};
  nextOverrideMap[soundName] = absPath;
  activeTheme._soundOverrideFiles = nextOverrideMap;
}

function mapAgentMetadata(agent) {
  return {
    id: agent.id,
    name: agent.name,
    eventSource: agent.eventSource,
    capabilities: agent.capabilities || {},
  };
}

function registerSettingsIpc(options = {}) {
  const ipcMain = requiredDependency(options.ipcMain, "ipcMain");
  const settingsController = requiredDependency(options.settingsController, "settingsController");
  const themeLoader = requiredDependency(options.themeLoader, "themeLoader");
  const codexPetMain = requiredDependency(options.codexPetMain, "codexPetMain");
  const dialog = requiredDependency(options.dialog, "dialog");
  const shell = requiredDependency(options.shell, "shell");
  const app = requiredDependency(options.app, "app");
  const BrowserWindow = requiredDependency(options.BrowserWindow, "BrowserWindow");
  const fs = options.fs || defaultFs;
  const path = options.path || defaultPath;
  const getSettingsWindow = options.getSettingsWindow || (() => null);
  const getActiveTheme = options.getActiveTheme || (() => null);
  const getLang = options.getLang || (() => "en");
  const settingsSizePreviewSession = requiredDependency(
    options.settingsSizePreviewSession,
    "settingsSizePreviewSession"
  );
  const isValidSizePreviewKey = requiredDependency(
    options.isValidSizePreviewKey,
    "isValidSizePreviewKey"
  );
  const sendToRenderer = options.sendToRenderer || (() => {});
  const getDoNotDisturb = options.getDoNotDisturb || (() => false);
  const getSoundMuted = options.getSoundMuted || (() => false);
  const getSoundVolume = options.getSoundVolume || (() => 1);
  const previewTextScale = options.previewTextScale
    || (() => ({ status: "error", message: "text scale preview unavailable" }));
  const endTextScalePreview = options.endTextScalePreview
    || (() => ({ status: "error", message: "text scale preview unavailable" }));
  const getTextScaleContext = options.getTextScaleContext
    || (() => ({ percent: 100 }));
  const getAllAgents = requiredDependency(options.getAllAgents, "getAllAgents");
  const detectAgentInstallations = options.detectAgentInstallations || defaultDetectAgentInstallations;
  const checkForUpdates = options.checkForUpdates || (() => {});
  const getHardwareBuddyStatus = options.getHardwareBuddyStatus || (() => null);
  const testHardwareBuddyApproval = options.testHardwareBuddyApproval || (async () => ({
    status: "error",
    message: "Hardware Buddy test approval is unavailable",
  }));
  const getQuickCommandPresets = options.getQuickCommandPresets || (() => ({
    enabled: false,
    presets: [],
  }));
  const sendQuickCommand = options.sendQuickCommand || (() => ({
    status: "error",
    code: "quick_commands_unavailable",
    message: "Quick Commands are unavailable",
  }));
  const now = options.now || (() => Date.now());
  const aboutHeroSvgPath = options.aboutHeroSvgPath
    || path.join(__dirname, "..", "assets", "svg", "minicpm-logo.svg");
  const disposers = [];

  function handle(channel, listener) {
    ipcMain.handle(channel, listener);
    disposers.push(() => ipcMain.removeHandler(channel));
  }

  function sanitizeQuickCommandPayload(payload) {
    const object = payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
    return {
      id: object.id,
      clientRequestId: object.clientRequestId,
    };
  }

  function getDialogParent(event) {
    return getSettingsDialogParent(event, { BrowserWindow, getSettingsWindow });
  }

  handle("settings:get-snapshot", () => settingsController.getSnapshot());
  handle("settings:update", (_event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "settings:update payload must be { key, value }" };
    }
    if (payload.key === "tgMigration") {
      return { status: "error", message: "tgMigration is internal; use telegramMigration.dispatch" };
    }
    // DANGER "auto-pilot": never let a plain settings:update flip this on. It
    // must go through the setAutoApproveAll command, which demands confirmed:true.
    // This makes the confirmation dialog a real boundary instead of UI-only.
    if (payload.key === "autoApproveAllPermissions") {
      return { status: "error", message: "autoApproveAllPermissions is gated; use the setAutoApproveAll command" };
    }
    return settingsController.applyUpdate(payload.key, payload.value);
  });
  handle("settings:begin-size-preview", () => settingsSizePreviewSession.begin());
  handle("settings:preview-size", (_event, value) => {
    if (!isValidSizePreviewKey(value)) {
      return { status: "error", message: `invalid preview size "${value}"` };
    }
    return settingsSizePreviewSession.preview(value).then(() => ({ status: "ok" }));
  });
  handle("settings:end-size-preview", (_event, value) => {
    if (value !== null && value !== undefined && !isValidSizePreviewKey(value)) {
      return { status: "error", message: `invalid preview size "${value}"` };
    }
    return settingsSizePreviewSession.end(value || null);
  });
  // Transient textScale preview while the slider drags: applies zoom to live
  // windows but never writes the store. Commit still goes through
  // settings:update so the controller stays the only writer.
  handle("settings:preview-text-scale", (_event, value) => {
    const n = Number(value);
    if (!Number.isFinite(n)) {
      return { status: "error", message: `invalid text scale "${value}"` };
    }
    return previewTextScale(n);
  });
  handle("settings:end-text-scale-preview", () => endTextScalePreview());
  // textScale is per-display; the renderer can't resolve its own display, so
  // the slider asks main for the committed value of the display the settings
  // window currently sits on.
  handle("settings:get-text-scale-context", () => getTextScaleContext());
  handle("settings:get-preview-sound-url", () => {
    try { return themeLoader.getPreviewSoundUrl(); }
    catch { return null; }
  });
  handle("settings:command", async (_event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "settings:command payload must be { action, payload }" };
    }
    return settingsController.applyCommand(payload.action, payload.payload);
  });

  handle("settings:pick-sound-file", async (event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "pickSoundFile payload must be an object" };
    }
    const { soundName } = payload;
    if (typeof soundName !== "string" || !soundName) {
      return { status: "error", message: "pickSoundFile.soundName must be a non-empty string" };
    }
    if (!/^[a-zA-Z0-9_-]+$/.test(soundName)) {
      return { status: "error", message: `pickSoundFile.soundName "${soundName}" contains invalid characters` };
    }

    const activeTheme = getActiveTheme();
    if (!activeTheme) return { status: "error", message: "no active theme" };
    const themeId = activeTheme._id;
    if (!isPlainObject(activeTheme.sounds) || !activeTheme.sounds[soundName]) {
      return { status: "error", message: `sound "${soundName}" not declared by theme "${themeId}"` };
    }
    const overridesDir = themeLoader.getSoundOverridesDir(themeId);
    if (!overridesDir) return { status: "error", message: "sound-overrides directory unavailable" };

    const lang = getLang();
    const strings = SOUND_OVERRIDE_DIALOG_STRINGS[lang] || SOUND_OVERRIDE_DIALOG_STRINGS.en;
    const extList = [...SOUND_OVERRIDE_ASSET_EXTS].map((ext) => ext.slice(1));
    let result;
    try {
      result = await dialog.showOpenDialog(getDialogParent(event), {
        title: strings.title,
        filters: [{ name: strings.filterName, extensions: extList }],
        properties: ["openFile"],
      });
    } catch (err) {
      return { status: "error", message: `pick dialog failed: ${err && err.message}` };
    }
    if (result.canceled || !result.filePaths || !result.filePaths[0]) {
      return { status: "cancel" };
    }

    const sourcePath = result.filePaths[0];
    const ext = path.extname(sourcePath).toLowerCase();
    if (!SOUND_OVERRIDE_ASSET_EXTS.has(ext)) {
      return { status: "error", message: `unsupported audio extension: ${ext || "(none)"}` };
    }

    try { fs.mkdirSync(overridesDir, { recursive: true }); }
    catch (err) { return { status: "error", message: `mkdir failed: ${err && err.message}` }; }

    const destFilename = `${soundName}${ext}`;
    const destPath = path.join(overridesDir, destFilename);
    try {
      fs.copyFileSync(sourcePath, destPath);
    } catch (err) {
      return { status: "error", message: `copy failed: ${err && err.message}` };
    }
    cleanupSiblingSoundOverrides(fs, path, overridesDir, soundName, ext);

    const cmdResult = await settingsController.applyCommand("setSoundOverride", {
      themeId,
      soundName,
      file: destFilename,
      originalName: path.basename(sourcePath),
    });
    if (!cmdResult || cmdResult.status !== "ok") {
      return cmdResult || { status: "error", message: "setSoundOverride failed" };
    }
    rememberRuntimeSoundOverrideFile({ getActiveTheme }, themeId, soundName, destPath);
    const newUrl = themeLoader.getSoundUrl(soundName);
    if (newUrl) {
      sendToRenderer("invalidate-sound-cache", newUrl);
      sendToRenderer("preload-sounds", { urls: [newUrl] });
    }
    return { status: "ok", file: destFilename };
  });

  handle("settings:preview-sound", (_event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "previewSound payload must be an object" };
    }
    const { soundName } = payload;
    if (typeof soundName !== "string" || !soundName) {
      return { status: "error", message: "previewSound.soundName must be a non-empty string" };
    }
    if (getDoNotDisturb()) return { status: "skipped", reason: "dnd" };
    if (getSoundMuted()) return { status: "skipped", reason: "muted" };
    const url = themeLoader.getSoundUrl(soundName);
    if (!url) return { status: "error", message: "sound unavailable" };
    const bustedUrl = `${url}${url.includes("?") ? "&" : "?"}_t=${now()}`;
    sendToRenderer("play-sound", { url: bustedUrl, volume: getSoundVolume() });
    return { status: "ok" };
  });

  handle("settings:open-sound-overrides-dir", async () => {
    const activeTheme = getActiveTheme();
    if (!activeTheme) return { status: "error", message: "no active theme" };
    const dir = themeLoader.getSoundOverridesDir(activeTheme._id);
    if (!dir) return { status: "error", message: "sound-overrides directory unavailable" };
    try { fs.mkdirSync(dir, { recursive: true }); } catch {}
    const openResult = await shell.openPath(dir);
    if (openResult) return { status: "error", message: openResult };
    return { status: "ok", path: dir };
  });

  handle("settings:list-themes", () => {
    try {
      const activeTheme = getActiveTheme();
      const activeId = activeTheme ? activeTheme._id : DEFAULT_THEME_ID;
      return themeLoader.listThemesWithMetadata().map((theme) =>
        codexPetMain.decorateThemeMetadata({
          ...theme,
          active: theme.id === activeId,
        })
      );
    } catch (err) {
      console.warn("Clawd: settings:list-themes failed:", err && err.message);
      return [];
    }
  });

  // Returns the emotion → animation mapping for a given theme's attention
  // state. Used by the Theme settings tab to show which GIFs play for each
  // [EMOTION:xxx] tag the LLM emits.
  handle("settings:get-theme-emotion-map", (event, { themeId } = {}) => {
    try {
      const id = typeof themeId === "string" && themeId ? themeId : DEFAULT_THEME_ID;
      const theme = themeLoader.loadTheme(id);
      if (!theme || !theme.states || !theme.states.attention) return { status: "ok", map: [] };
      const files = Array.isArray(theme.states.attention) ? theme.states.attention : [];
      const map = [];
      const seen = new Set();
      for (const entry of files) {
        if (typeof entry === "object" && entry && entry.file && entry.emotion) {
          const emo = String(entry.emotion).toLowerCase();
          if (!seen.has(emo)) {
            seen.add(emo);
            map.push({ emotion: emo, file: entry.file, isDefault: false });
          }
        } else if (typeof entry === "string") {
          if (!seen.has("neutral")) {
            seen.add("neutral");
            map.push({ emotion: "neutral", file: entry, isDefault: true });
          }
        }
      }
      // Load user overrides from {userData}/emotion-overrides/{themeId}.json
      // so uploaded custom animations show up alongside the theme's built-in
      // entries. Overrides with the same emotion name replace the built-in.
      let overrides = {};
      try {
        const root = typeof themeLoader.getEmotionOverridesRoot === "function"
          ? themeLoader.getEmotionOverridesRoot() : null;
        if (root) {
          const manifestPath = path.join(root, `${id}.json`);
          try { overrides = JSON.parse(fs.readFileSync(manifestPath, "utf-8")); } catch {}
        }
      } catch {}
      // Merge overrides: replace built-in entry if same emotion, otherwise append
      for (const [emo, ov] of Object.entries(overrides)) {
        if (!ov || typeof ov.file !== "string") continue;
        const idx = map.findIndex((m) => m.emotion === emo);
        if (idx >= 0) {
          map[idx] = { emotion: emo, file: ov.file, isDefault: false, isOverride: true };
        } else {
          map.push({ emotion: emo, file: ov.file, isDefault: false, isOverride: true });
        }
      }
      return { status: "ok", map, themeDir: theme._themeDir || null };
    } catch (err) {
      console.warn("Clawd: settings:get-theme-emotion-map failed:", err && err.message);
      return { status: "error", message: String(err && err.message), map: [] };
    }
  });

  // Emotion animation file picker — lets the user upload a custom GIF/APNG/
  // SVG/PNG/WebP file for a specific emotion tag. Follows the sound-override
  // pattern: native file dialog → copy into userData dir → update prefs.
  const EMOTION_ANIMATION_ASSET_EXTS = new Set([".gif", ".apng", ".svg", ".png", ".webp", ".jpg", ".jpeg"]);
  handle("settings:pick-emotion-animation", async (event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "pickEmotionAnimation payload must be an object" };
    }
    const { emotionName } = payload;
    if (typeof emotionName !== "string" || !emotionName) {
      return { status: "error", message: "pickEmotionAnimation.emotionName must be a non-empty string" };
    }
    if (!/^[a-zA-Z0-9_-]+$/.test(emotionName)) {
      return { status: "error", message: `emotionName "${emotionName}" contains invalid characters` };
    }

    const activeTheme = getActiveTheme();
    if (!activeTheme) return { status: "error", message: "no active theme" };
    const themeId = activeTheme._id;
    const overridesDir = typeof themeLoader.getEmotionOverridesDir === "function"
      ? themeLoader.getEmotionOverridesDir(themeId) : null;
    if (!overridesDir) return { status: "error", message: "emotion-overrides directory unavailable" };

    const lang = getLang();
    const strings = {
      en: { title: "Choose an emotion animation", filterName: "Animations" },
      zh: { title: "选择情绪动画文件", filterName: "动画" },
      "zh-TW": { title: "選擇情緒動畫檔案", filterName: "動畫" },
    };
    const loc = strings[lang] || strings.en;
    const extList = [...EMOTION_ANIMATION_ASSET_EXTS].map((ext) => ext.slice(1));
    let result;
    try {
      result = await dialog.showOpenDialog(getDialogParent(event), {
        title: loc.title,
        filters: [{ name: loc.filterName, extensions: extList }],
        properties: ["openFile"],
      });
    } catch (err) {
      return { status: "error", message: `pick dialog failed: ${err && err.message}` };
    }
    if (result.canceled || !result.filePaths || !result.filePaths[0]) {
      return { status: "cancel" };
    }

    const sourcePath = result.filePaths[0];
    const ext = path.extname(sourcePath).toLowerCase();
    if (!EMOTION_ANIMATION_ASSET_EXTS.has(ext)) {
      return { status: "error", message: `unsupported extension: ${ext || "(none)"}` };
    }

    try { fs.mkdirSync(overridesDir, { recursive: true }); }
    catch (err) { return { status: "error", message: `mkdir failed: ${err && err.message}` }; }

    const destFilename = `${emotionName}${ext}`;
    const destPath = path.join(overridesDir, destFilename);
    try {
      fs.copyFileSync(sourcePath, destPath);
    } catch (err) {
      return { status: "error", message: `copy failed: ${err && err.message}` };
    }
    return { status: "ok", themeId, emotionName, file: destFilename, absPath: destPath };
  });

  // Save an emotion override — records the uploaded file in a per-theme JSON,
  // merged into the emotion map returned by get-theme-emotion-map.
  // Forward a proactive-chat policy change to the chat renderer.
  // policy = "off" | "free" | "interval:<seconds>"
  handle("settings:set-proactive-policy", (event, { policy } = {}) => {
    try {
      const chat = typeof options.getMinicpmChat === "function" ? options.getMinicpmChat() : null;
      if (chat && typeof chat.setProactivePolicy === "function") {
        chat.setProactivePolicy(String(policy || "free"));
        return { status: "ok" };
      }
      return { status: "error", message: "minicpm chat not available" };
    } catch (err) {
      return { status: "error", message: String(err && err.message) };
    }
  });

  handle("settings:save-emotion-override", async (event, payload) => {
    if (!payload || typeof payload !== "object") {
      return { status: "error", message: "saveEmotionOverride payload must be an object" };
    }
    const { themeId, emotionName, file } = payload;
    if (typeof themeId !== "string" || !themeId) {
      return { status: "error", message: "themeId required" };
    }
    if (typeof emotionName !== "string" || !emotionName) {
      return { status: "error", message: "emotionName required" };
    }
    const overridesDir = typeof themeLoader.getEmotionOverridesRoot === "function"
      ? themeLoader.getEmotionOverridesRoot() : null;
    if (!overridesDir) return { status: "error", message: "emotion-overrides root unavailable" };
    const manifestPath = path.join(overridesDir, `${themeId}.json`);
    let manifest = {};
    try { manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8")); } catch {}
    if (file === null) {
      delete manifest[emotionName];
    } else {
      manifest[emotionName] = { file };
    }
    try {
      fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), "utf-8");
    } catch (err) {
      return { status: "error", message: `write manifest failed: ${err && err.message}` };
    }
    return { status: "ok" };
  });

  handle("settings:open-user-themes-dir", async () => {
    const dir = typeof themeLoader.ensureUserThemesDir === "function"
      ? themeLoader.ensureUserThemesDir()
      : null;
    if (!dir) return { status: "error", message: "user themes directory unavailable" };
    const openResult = await shell.openPath(dir);
    if (openResult) return { status: "error", message: openResult };
    return { status: "ok", path: dir };
  });

  handle("settings:import-user-theme-zip", async (event) => {
    let result;
    try {
      result = await dialog.showOpenDialog(getDialogParent(event), {
        properties: ["openFile"],
        filters: [{ name: "Desk Pet theme zip", extensions: ["zip"] }],
      });
    } catch (err) {
      return { status: "error", message: `theme zip picker failed: ${err && err.message}` };
    }
    if (!result || result.canceled || !result.filePaths || !result.filePaths[0]) {
      return { status: "cancel" };
    }

    try {
      const userThemesDir = typeof themeLoader.ensureUserThemesDir === "function"
        ? themeLoader.ensureUserThemesDir()
        : null;
      return settingsThemeImporter.importUserThemeZip(result.filePaths[0], {
        fs,
        path,
        userThemesDir,
      });
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  handle("settings:refresh-codex-pets", () => codexPetMain.refreshFromSettings());
  handle("settings:open-codex-pets-dir", () => codexPetMain.openCodexPetsDir());
  handle("settings:import-codex-pet-zip", (event) => codexPetMain.importCodexPetZip(event));
  handle("settings:remove-codex-pet", (_event, themeId) => codexPetMain.removeCodexPet(themeId));

  handle("settings:confirm-remove-theme", async (event, themeId) => {
    if (typeof themeId !== "string" || !themeId) return { confirmed: false };
    const meta = themeLoader.getThemeMetadata(themeId);
    const displayName = (meta && meta.name) || themeId;
    const lang = getLang();
    const strings = REMOVE_THEME_DIALOG_STRINGS[lang] || REMOVE_THEME_DIALOG_STRINGS.en;
    try {
      const { response } = await dialog.showMessageBox(getDialogParent(event), {
        type: "warning",
        buttons: [strings.delete, strings.cancel],
        defaultId: 1,
        cancelId: 1,
        message: strings.message(displayName),
        detail: strings.detail,
        noLink: true,
      });
      return { confirmed: response === 0 };
    } catch (err) {
      console.warn("Clawd: confirm-remove-theme dialog failed:", err && err.message);
      return { confirmed: false };
    }
  });

  handle("settings:list-agents", () => {
    try {
      return getAllAgents().map(mapAgentMetadata);
    } catch (err) {
      console.warn("Clawd: settings:list-agents failed:", err && err.message);
      return [];
    }
  });

  handle("settings:detect-agent-installations", () => {
    try {
      return detectAgentInstallations({ fs, path, now });
    } catch (err) {
      console.warn("Clawd: settings:detect-agent-installations failed:", err && err.message);
      return {
        checkedAt: now(),
        agents: [],
        skippedAgentIds: [],
        error: err && err.message ? err.message : String(err),
      };
    }
  });

  handle("settings:get-about-info", () => {
    let heroSvgContent = "";
    try {
      heroSvgContent = fs.readFileSync(aboutHeroSvgPath, "utf8");
    } catch (err) {
      console.warn("Clawd: failed to read about hero SVG:", err && err.message);
    }
    let pendingUpdateVersion = "";
    let autoUpdateCheck = true;
    try {
      pendingUpdateVersion = String(settingsController.get("pendingUpdateVersion") || "");
      autoUpdateCheck = settingsController.get("autoUpdateCheck") !== false;
    } catch {}
    return {
      version: app.getVersion(),
      appName: productMetadata.appDisplayName,
      repoUrl: productMetadata.repoUrl,
      license: productMetadata.licenseId,
      copyright: productMetadata.copyrightLine,
      upstreamRepoUrl: productMetadata.upstreamRepoUrl,
      upstreamLabel: productMetadata.upstreamLabel,
      heroSvgContent,
      pendingUpdateVersion,
      autoUpdateCheck,
    };
  });

  handle("settings:check-for-updates", () => {
    try {
      checkForUpdates(true);
      return { status: "ok" };
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  handle("settings:engine-local-version", async () => {
    // Local engine build WITHOUT the sidecar: runs llama-server --version
    // directly. Used so the settings UI can always show the installed
    // engine version even when the gateway is down.
    const bin = locateLlamaServer();
    if (!bin) return { status: "error", message: "llama-server not found" };
    try {
      const { execFile } = require("child_process");
      const out = await new Promise((resolve, reject) => {
        execFile(bin, ["--version"], { timeout: 5000 }, (err, stdout, stderr) => {
          if (err) reject(err);
          else resolve(String(stdout || stderr));
        });
      });
      const m = out.match(/version:\s*(\d+)/);
      return {
        status: "ok",
        build: m ? Number(m[1]) : null,
        raw: out.slice(0, 200),
      };
    } catch (err) {
      return { status: "error", message: err && err.message || String(err) };
    }
  });

  handle("settings:engine-update-check", async () => {
    const r = await sidecarJson("GET", "/api/engine-update-check", 3000);
    if (!r.ok) return { status: "error", message: r.error || "sidecar unreachable" };
    return { status: "ok", info: r.json || {} };
  });

  handle("settings:get-memory-view", async () => {
    // Settings → Memory viewer: identity notes + episodic events +
    // mood for the CURRENT theme (换身体 = 换灵魂).
    const [mem, events, mood] = await Promise.all([
      sidecarJson("GET", "/api/memory", 4000),
      sidecarJson("GET", "/api/events/list", 4000),
      sidecarJson("GET", "/api/mood", 4000),
    ]);
    return {
      status: "ok",
      identity: (mem && mem.json) || null,
      events: (events && events.json) || null,
      mood: (mood && mood.json) || null,
    };
  });

  handle("settings:sync-screen-consent", async (_event, value) => {
    // Push the "always allow screen" setting to the live sidecar (its
    // consent_state is process memory; the persisted pref is the truth).
    const consent = value === "always" ? "always" : "deny";
    const r = await sidecarJson("POST", "/api/screen/consent-status", 3000, { consent });
    if (!r.ok) return { status: "error", message: r.error || "sidecar unreachable" };
    return { status: "ok", current: r.json && r.json.current_consent };
  });

  handle("settings:engine-update-apply", async (event) => {
    // Forward every SSE phase to the settings window in real time so the
    // Model tab can paint download progress.
    return engineUpdateApplyCommon(event);
  });

  handle("settings:engine-update-apply-dir", async (event) => {
    // Offline update: pick a folder containing an engine copy (e.g.
    // someone else's downloaded release). Folder is copied, never moved.
    const parent = getSettingsDialogParent(event, { BrowserWindow, getSettingsWindow });
    const ret = await dialog.showOpenDialog(parent, {
      title: "选择引擎文件夹（含 llama-server 的官方发布包解压内容）",
      properties: ["openDirectory"],
      message: "选择包含 llama-server 的文件夹（官方 release 解压后的内容）",
    });
    if (ret.canceled || !ret.filePaths.length) {
      return { status: "canceled" };
    }
    return engineUpdateApplyCommon(event, { source_dir: ret.filePaths[0] });
  });

  async function engineUpdateApplyCommon(event, body) {
    // Forward every SSE phase to the settings window in real time so the
    // Model tab can paint progress.
    const r = await sidecarEngineApply(0, (ev) => {
      try {
        const win = getSettingsWindow();
        if (win && !win.isDestroyed()) {
          win.webContents.send("minicpm:engine-update-progress", ev);
        }
      } catch {}
    }, body ? { path: "/api/engine-update-apply-dir", body } : null);
    if (!r.ok) return { status: "error", message: r.error || "sidecar unreachable" };
    if (r.error) return { status: "error", message: r.error };
    return { status: "ok", phases: r.phases };
  }

  handle("settings:get-hardware-buddy-status", () => getHardwareBuddyStatus());
  handle("settings:test-hardware-buddy-approval", () => testHardwareBuddyApproval());
  handle("settings:get-quick-command-presets", () => getQuickCommandPresets());
  handle("settings:send-quick-command", (_event, payload) => sendQuickCommand(sanitizeQuickCommandPayload(payload)));

  handle("settings:open-external", async (_event, url) => {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
      return { status: "error", message: "Invalid URL" };
    }
    try {
      await shell.openExternal(url);
      return { status: "ok" };
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  handle("settings:mobile-connection-info", async () => {
    try {
      const lanWsServer = options.getLanWsServer ? options.getLanWsServer() : null;
      if (!lanWsServer) return { status: "error", message: "LAN bridge not available" };
      const port = lanWsServer.getPort();
      const tok = lanWsServer.getToken();
      if (!Number.isInteger(port) || port <= 0 || typeof tok !== "string" || !tok) {
        return { status: "starting", message: "LAN bridge is starting" };
      }
      const os = require("os");
      let lanIp = "127.0.0.1";
      const interfaces = os.networkInterfaces();
      const wlanPattern = /WLAN|Wi-?Fi|Wireless|无线/i;
      // 1) 优先找 WLAN 接口
      for (const name of Object.keys(interfaces)) {
        if (wlanPattern.test(name)) {
          for (const iface of interfaces[name]) {
            if (iface.family === "IPv4" && !iface.internal) { lanIp = iface.address; break; }
          }
          if (lanIp !== "127.0.0.1") break;
        }
      }
      // 2) fallback：第一个非 internal IPv4
      if (lanIp === "127.0.0.1") {
        for (const name of Object.keys(interfaces)) {
          for (const iface of interfaces[name]) {
            if (iface.family === "IPv4" && !iface.internal) { lanIp = iface.address; break; }
          }
          if (lanIp !== "127.0.0.1") break;
        }
      }
      const pairUrl = `http://${lanIp}:${port}/mobile/?host=${lanIp}&port=${port}&token=${tok}`;
      return { status: "ok", port, token: tok, lanIp, pairUrl };
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  handle("settings:regenerate-mobile-token", async () => {
    try {
      const lanWsServer = options.getLanWsServer ? options.getLanWsServer() : null;
      if (!lanWsServer) return { status: "error", message: "LAN bridge not available" };
      const newToken = lanWsServer.regenerateToken();
      return { status: "ok", token: newToken };
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  handle("settings:reset-mobile-access", async () => {
    try {
      const lanWsServer = options.getLanWsServer ? options.getLanWsServer() : null;
      if (!lanWsServer) return { status: "error", message: "LAN bridge not available" };
      const newToken = lanWsServer.resetMobileAccess();
      return { status: "ok", token: newToken };
    } catch (err) {
      return { status: "error", message: (err && err.message) || String(err) };
    }
  });

  return {
    dispose() {
      while (disposers.length) {
        const dispose = disposers.pop();
        try { dispose(); } catch {}
      }
    },
  };
}

module.exports = {
  registerSettingsIpc,
};
