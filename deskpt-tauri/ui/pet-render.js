// pet-render.js — mini state engine driven by the ORIGINAL theme.json.
// Displays the same cybercat GIF assets the Electron shell shows: idle,
// drag reaction, poke/annoyed reactions. Layout constants come from the
// theme's objectScale (same ratios the original renderer applies).
(async function () {
  const { listen } = window.__TAURI__.event;
  const pet = document.getElementById("pet");
  const frame = document.getElementById("frame");

  const theme = await fetch("themes/cybercat/theme.json").then((r) => r.json());
  const scaleCfg = theme.objectScale || {};
  const ASSETS = "themes/cybercat/assets/";

  // State → asset file (subset of the original state machine; the visual
  // identity is the same assets).
  const STATE_FILE = {
    idle: "cybercat-idle.gif",
    walking: (theme.reactions && theme.reactions.drag) ? theme.reactions.drag.file : "cybercat-dragging.gif",
    dragging: theme.reactions && theme.reactions.drag ? theme.reactions.drag.file : "cybercat-dragging.gif",
    annoyed: theme.reactions && theme.reactions.annoyed ? theme.reactions.annoyed.file : "cybercat-annoying.gif",
  };
  const REACTION_MS = (theme.reactions && theme.reactions.annoyed && theme.reactions.annoyed.duration) || 3500;

  let current = "idle";
  let reactionTimer = null;

  function apply(state) {
    current = state;
    const file = STATE_FILE[state] || STATE_FILE.idle;
    // cache-bust replays the GIF animation from frame 0
    frame.src = ASSETS + file + "?v=" + Date.now();
    layout(file);
  }

  function layout(file) {
    // objectScale ratios (from the original theme spec): the image sits
    // anchored near the bottom of the window with theme-specified offsets.
    const W = window.innerWidth;
    const H = window.innerHeight;
    const w = Math.round(W * (scaleCfg.imgWidthRatio || 0.6));
    const h = w; // GIFs are square (500x500 viewBox)
    const offsets = (scaleCfg.fileOffsets && scaleCfg.fileOffsets[file]) || { x: 0, y: 0 };
    const scales = (scaleCfg.fileScales && scaleCfg.fileScales[file]) || 1;
    pet.style.width = Math.round(w * scales) + "px";
    pet.style.height = Math.round(h * scales) + "px";
    pet.style.left = Math.round(W * (scaleCfg.offsetX || 0.2)) + offsets.x + "px";
    pet.style.top = Math.round(H - h * scales - H * (scaleCfg.imgBottom || 0.05)) + offsets.y + "px";
  }

  window.addEventListener("resize", () => layout(STATE_FILE[current]));

  // Reactions pushed by the hit window (drag / poke) and the walk engine.
  await listen("pet-reaction", (e) => {
    const kind = (e.payload && e.payload.kind) || "annoyed";
    if (kind === "walk-start") {
      clearTimeout(reactionTimer);
      apply("walking");
      return;
    }
    if (kind === "walk-end") {
      clearTimeout(reactionTimer);
      apply("idle");
      return;
    }
    if (kind === "drag") {
      clearTimeout(reactionTimer);
      apply("dragging");
      return;
    }
    if (kind === "drag-end") {
      clearTimeout(reactionTimer);
      apply("idle");
      return;
    }
    // poke / annoyed: play for the theme duration, then back to idle
    clearTimeout(reactionTimer);
    apply(kind === "poke" ? "annoyed" : kind);
    reactionTimer = setTimeout(() => apply("idle"), REACTION_MS);
  });

  apply("idle");
})();
