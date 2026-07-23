"use strict";

const { VISUAL_FALLBACK_STATES } = require("./theme-loader");

function buildStateBindings(nextTheme) {
  const bindings = {};
  const sourceBindings = nextTheme && nextTheme._stateBindings;
  if (sourceBindings && typeof sourceBindings === "object") {
    for (const [stateKey, entry] of Object.entries(sourceBindings)) {
      bindings[stateKey] = {
        files: Array.isArray(entry && entry.files) ? [...entry.files] : [],
        fallbackTo: typeof (entry && entry.fallbackTo) === "string" && entry.fallbackTo ? entry.fallbackTo : null,
      };
    }
  }
  if (nextTheme && nextTheme.states) {
    for (const [stateKey, files] of Object.entries(nextTheme.states)) {
      const normalizedFiles = Array.isArray(files) ? [...files] : [];
      if (!bindings[stateKey]) {
        bindings[stateKey] = { files: normalizedFiles, fallbackTo: null };
      } else if (bindings[stateKey].files.length === 0) {
        bindings[stateKey].files = normalizedFiles;
      }
    }
  }
  if (nextTheme && nextTheme.miniMode && nextTheme.miniMode.states) {
    for (const [stateKey, files] of Object.entries(nextTheme.miniMode.states)) {
      bindings[stateKey] = {
        files: Array.isArray(files) ? [...files] : [],
        fallbackTo: null,
      };
    }
  }
  // Ensure "roam" binding exists — free-roam mode switches to this visual
  // state while walking. Themes that provide roam SVGs get them; others
  // fall back to idle so the pet at least shows the idle animation instead
  // of being "dragged" with no visual change.
  //
  // Also inject a placeholder into nextTheme.states so that
  // theme-variants.applyUserOverridesPatch can resolve the target collection
  // for "roam" overrides (otherwise it skips states not present in raw.states).
  if (!bindings.roam) {
    bindings.roam = { files: [], fallbackTo: "idle" };
  }
  if (nextTheme && nextTheme.states && !Array.isArray(nextTheme.states.roam)) {
    const idleDefault = (nextTheme.states.idle && Array.isArray(nextTheme.states.idle)
      && nextTheme.states.idle.length > 0)
      ? nextTheme.states.idle[0]
      : "idle.svg";
    nextTheme.states.roam = [idleDefault];
  }
  return bindings;
}

function pickStateFile(files, randomFn = Math.random) {
  if (!Array.isArray(files) || files.length === 0) return null;
  const random = typeof randomFn === "function" ? randomFn : Math.random;
  return files[Math.floor(random() * files.length)];
}

// Normalize a single file entry from a theme's states.<state> array.
// Entries may be either a plain filename string ("attention.svg") or an
// object { file, emotion } declaring an emotion-specific variant. Object
// entries keep the file path itself as the canonical string; the emotion
// is a side-channel consulted by emotion-aware selection below.
function normalizeEntry(entry) {
  if (entry == null) return null;
  if (typeof entry === "string") return { file: entry, emotion: null };
  if (typeof entry === "object" && !Array.isArray(entry)) {
    const file = typeof entry.file === "string" ? entry.file : null;
    if (!file) return null;
    const emo = typeof entry.emotion === "string" && /^[a-z_]+$/.test(entry.emotion)
      ? entry.emotion.toLowerCase()
      : null;
    return { file, emotion: emo };
  }
  return null;
}

// Pick a file from a state binding, preferring emotion-tagged variants when
// `emotion` is a non-neutral mood AND the binding actually has at least one
// variant for that mood. Otherwise fall through to plain string entries
// (variants without an emotion tag), then to the whole pool as a last
// resort. This makes emotion variants purely additive — a theme that
// doesn't declare any still works exactly as before.
function pickStateFileByEmotion(files, emotion, randomFn = Math.random) {
  if (!Array.isArray(files) || files.length === 0) return null;
  const random = typeof randomFn === "function" ? randomFn : Math.random;
  const normalized = files.map(normalizeEntry).filter(Boolean);
  if (normalized.length === 0) return null;

  if (emotion && emotion !== "neutral") {
    const tagged = normalized.filter((e) => e.emotion === emotion).map((e) => e.file);
    if (tagged.length > 0) return tagged[Math.floor(random() * tagged.length)];
  }
  // Prefer entries without an emotion tag — those are the "neutral" frames
  // for this state. Only fall back to other moods' tagged frames when no
  // neutral frame exists, so we never refuse to render something.
  const neutral = normalized.filter((e) => !e.emotion).map((e) => e.file);
  if (neutral.length > 0) return neutral[Math.floor(random() * neutral.length)];
  return normalized[Math.floor(random() * normalized.length)].file;
}

function hasOwnVisualFiles(stateBindings, state) {
  const entry = stateBindings && stateBindings[state];
  if (!entry) return false;
  if (!Array.isArray(entry.files) || entry.files.length === 0) return false;
  // Object entries {file, emotion} count as real files too — a strings-only
  // check would wrongly report "no own visual files" on emotions-tagged themes.
  return entry.files.some((f) => {
    if (typeof f === "string") return f.length > 0;
    return f && typeof f === "object" && typeof f.file === "string" && f.file.length > 0;
  });
}

function resolveVisualBinding(state, stateBindings, options = {}) {
  const pickFile = typeof options.pickStateFile === "function" ? options.pickStateFile : pickStateFile;
  // Emotion preference — when set and not "neutral", the resolver asks the
  // picker for a variant tagged with this mood. pickStateFileByEmotion falls
  // back gracefully if no tagged variants exist.
  const emotion = typeof options.emotion === "string" && /^[a-z_]+$/.test(options.emotion)
    ? options.emotion.toLowerCase()
    : null;
  const pickWithEmotion = (files) => pickStateFileByEmotion(files, emotion);
  let cursor = state;
  let visited = null;
  for (let hops = 0; hops <= 3; hops += 1) {
    const entry = stateBindings && stateBindings[cursor];
    if (entry && Array.isArray(entry.files) && entry.files.length > 0) {
      // Respect a custom pickStateFile override (the legacy contract) but
      // default to the emotion-aware picker so themes with {file, emotion}
      // variants actually resolve to a mood-appropriate animation.
      if (typeof options.pickStateFile === "function") return pickFile(entry.files);
      return pickWithEmotion(entry.files);
    }
    if (!entry || !entry.fallbackTo || !VISUAL_FALLBACK_STATES.has(cursor)) break;
    if (!visited) visited = new Set([cursor]);
    if (visited.has(entry.fallbackTo)) break;
    visited.add(entry.fallbackTo);
    cursor = entry.fallbackTo;
  }
  const idleEntry = stateBindings && stateBindings.idle;
  if (idleEntry && Array.isArray(idleEntry.files) && idleEntry.files.length > 0) {
    if (typeof options.pickStateFile === "function") return pickFile(idleEntry.files);
    return pickWithEmotion(idleEntry.files);
  }
  return null;
}

function normalizeSessionsIterable(sessions) {
  if (!sessions) return [];
  if (sessions instanceof Map) return sessions.entries();
  if (typeof sessions[Symbol.iterator] === "function") return sessions;
  return [];
}

function countActiveSessionsByStates(sessions, states) {
  let count = 0;
  for (const [, session] of normalizeSessionsIterable(sessions)) {
    if (!session.headless && states.has(session.state)) count += 1;
  }
  return count;
}

function selectTieredStateFile(tiers, count, fallbackFile) {
  if (tiers) {
    for (const tier of tiers) {
      if (count >= tier.minSessions) return tier.file;
    }
  }
  return fallbackFile;
}

// Flatten a state-files array (which may contain plain strings or
// {file, emotion} objects) into the plain filename strings downstream
// consumers expect. Used to normalize theme.states into STATE_SVGS so
// tiered-state lookups, hitbox resolution, eye-tracking validation, etc.
// keep working when an author declares emotion-tagged variants.
function filesToNames(files) {
  if (!Array.isArray(files)) return [];
  const names = [];
  for (const f of files) {
    const e = normalizeEntry(f);
    if (e && e.file) names.push(e.file);
  }
  return names;
}

function getWorkingSvg(options = {}) {
  const count = countActiveSessionsByStates(
    options.sessions,
    new Set(["working", "thinking", "juggling"])
  );
  const stateSvgs = options.stateSvgs;
  return selectTieredStateFile(
    options.theme && options.theme.workingTiers,
    count,
    stateSvgs.working[0]
  );
}

function getJugglingSvg(options = {}) {
  const count = countActiveSessionsByStates(
    options.sessions,
    new Set(["juggling"])
  );
  const stateSvgs = options.stateSvgs;
  return selectTieredStateFile(
    options.theme && options.theme.jugglingTiers,
    count,
    stateSvgs.juggling[0]
  );
}

function getWinningSessionDisplayHint(sessions, targetState, displayHintMap = {}) {
  let best = null;
  let bestAt = -1;
  for (const [, session] of normalizeSessionsIterable(sessions)) {
    if (session.headless || session.state !== targetState) continue;
    if (session.updatedAt >= bestAt) {
      bestAt = session.updatedAt;
      best = session;
    }
  }
  if (!best || !best.displayHint) return null;
  const resolved = displayHintMap[best.displayHint];
  return resolved || null;
}

function getSvgOverride(state, options = {}) {
  if (options.updateVisualState && state === options.updateVisualState && options.updateVisualSvgOverride) {
    return options.updateVisualSvgOverride;
  }
  if (state === "idle") return options.idleFollowSvg;
  if (state === "working") {
    const hinted = getWinningSessionDisplayHint(options.sessions, "working", options.displayHintMap);
    if (hinted) return hinted;
    return getWorkingSvg(options);
  }
  if (state === "juggling") {
    const hinted = getWinningSessionDisplayHint(options.sessions, "juggling", options.displayHintMap);
    if (hinted) return hinted;
    return getJugglingSvg(options);
  }
  if (state === "thinking") {
    const hinted = getWinningSessionDisplayHint(options.sessions, "thinking", options.displayHintMap);
    if (hinted) return hinted;
    const stateSvgs = options.stateSvgs;
    return stateSvgs.thinking[0];
  }
  return null;
}

module.exports = {
  buildStateBindings,
  pickStateFile,
  pickStateFileByEmotion,
  normalizeEntry,
  filesToNames,
  hasOwnVisualFiles,
  resolveVisualBinding,
  countActiveSessionsByStates,
  selectTieredStateFile,
  getWorkingSvg,
  getJugglingSvg,
  getWinningSessionDisplayHint,
  getSvgOverride,
};
