/**
 * LingLing micro-animations — token-level emotional expression.
 *
 * The model outputs specific characters/punctuation that trigger
 * micro-animations in real time during streaming. This creates
 * a continuous "emotion stream" — the pet doesn't just have a
 * mood, it performs its feelings character by character.
 *
 * Architecture:
 *   - Macro: [EMOTION:xxx] tag controls overall animation state
 *   - Micro: Individual characters trigger subtle animations
 *   - Combo: Character sequences trigger complex animation chains
 */

// ── Single-character triggers ───────────────────────────────────────

const MICRO_ANIMATIONS = {
  "！": "startle",
  "？": "tilt_head",
  "…": "hesitate",
  "～": "sway",
  "。": "settle",
  "嗯": "nod",
  "哼": "turn_away",
  "呜": "shrink",
  "诶": "perk_up",
};

// ── Combo triggers (checked before single chars) ────────────────────

const COMBO_ANIMATIONS = {
  "……！": "hesitate_then_burst",
  "！？": "shock_confused",
  "……～": "shy_drift",
  "……。": "quiet_acceptance",
  "！～": "excited_sway",
};

// ── Mood → physical parameter mapping ───────────────────────────────

const MOOD_PARAMS = {
  "开心": { color: "+warm", speed: 1.3, bounce: true, glow: 0.7 },
  "平静": { color: "none", speed: 1.0, bounce: false, glow: 0.3 },
  "难过": { color: "+cool", speed: 0.6, bounce: false, glow: 0.1 },
  "生气": { color: "+red", speed: 1.5, bounce: false, glow: 0.9, shake: true },
  "好奇": { color: "+cyan", speed: 1.2, bounce: true, glow: 0.5 },
  "兴奋": { color: "+warm", speed: 1.4, bounce: true, glow: 0.8 },
  "害怕": { color: "+cool", speed: 0.8, bounce: false, glow: 0.2, shrink: true },
};

// ── Animation engine ────────────────────────────────────────────────

let _buffer = "";
let _currentMood = "平静";
let _petElement = null;

/**
 * Initialize the micro-animation engine.
 * @param {HTMLElement} petEl - The pet container element (#pet-container or #clawd)
 */
export function initMicroAnimations(petEl) {
  _petElement = petEl;
  _buffer = "";
}

/**
 * Set the current mood (from mood.json via /api/mood).
 * Adjusts the baseline animation parameters.
 * @param {string} mood - Mood name (开心/平静/难过/生气/好奇/兴奋/害怕)
 */
export function setMood(mood) {
  _currentMood = mood;
  applyMoodParams();
}

/**
 * Process a streaming token. Call for each SSE delta event.
 * @param {string} token - The text token from the stream
 */
export function onToken(token) {
  if (!_petElement) return;

  _buffer += token;

  // Check combos first (longer patterns take priority)
  for (const [pattern, anim] of Object.entries(COMBO_ANIMATIONS)) {
    if (_buffer.endsWith(pattern)) {
      triggerMicroAnimation(anim);
      _buffer = _buffer.slice(0, -pattern.length);
      return;
    }
  }

  // Check single characters
  const lastChar = token.slice(-1);
  if (MICRO_ANIMATIONS[lastChar]) {
    triggerMicroAnimation(MICRO_ANIMATIONS[lastChar]);
  }
}

/**
 * Reset the buffer. Call at conversation start.
 */
export function resetBuffer() {
  _buffer = "";
}

// ── Internal ────────────────────────────────────────────────────────

function triggerMicroAnimation(name) {
  if (!_petElement) return;

  // Remove any existing micro-animation class
  _petElement.classList.remove(
    ...Object.values(MICRO_ANIMATIONS).map(a => `micro-${a}`),
    ...Object.values(COMBO_ANIMATIONS).map(a => `micro-${a}`)
  );

  // Force reflow to restart animation
  void _petElement.offsetWidth;

  // Apply new animation class
  _petElement.classList.add(`micro-${name}`);

  // Auto-remove after animation completes
  const duration = getAnimationDuration(name);
  setTimeout(() => {
    _petElement.classList.remove(`micro-${name}`);
  }, duration);
}

function getAnimationDuration(name) {
  const durations = {
    startle: 400,
    tilt_head: 600,
    hesitate: 800,
    sway: 1200,
    settle: 500,
    nod: 400,
    turn_away: 600,
    shrink: 500,
    perk_up: 400,
    hesitate_then_burst: 1500,
    shock_confused: 800,
    shy_drift: 1200,
    quiet_acceptance: 800,
    excited_sway: 1000,
  };
  return durations[name] || 500;
}

function applyMoodParams() {
  if (!_petElement) return;
  const params = MOOD_PARAMS[_currentMood] || MOOD_PARAMS["平静"];

  // Apply CSS custom properties for mood-based adjustments
  _petElement.style.setProperty("--mood-speed", params.speed);
  _petElement.style.setProperty("--mood-glow", params.glow);

  if (params.color !== "none") {
    _petElement.style.setProperty("--mood-color-shift", params.color);
  } else {
    _petElement.style.removeProperty("--mood-color-shift");
  }

  if (params.bounce) {
    _petElement.classList.add("mood-bounce");
  } else {
    _petElement.classList.remove("mood-bounce");
  }

  if (params.shake) {
    _petElement.classList.add("mood-shake");
  } else {
    _petElement.classList.remove("mood-shake");
  }

  if (params.shrink) {
    _petElement.classList.add("mood-shrink");
  } else {
    _petElement.classList.remove("mood-shrink");
  }
}

/**
 * Inject the micro-animation CSS into the document.
 * Call once during initialization.
 */
export function injectMicroAnimationStyles() {
  if (document.getElementById("micro-animations-css")) return;

  const style = document.createElement("style");
  style.id = "micro-animations-css";
  style.textContent = `
    /* LingLing micro-animations — token-level emotional expression */

    /* Single-character animations */
    .micro-startle { animation: micro-startle 0.4s ease-out; }
    @keyframes micro-startle {
      0% { transform: scale(1); }
      30% { transform: scale(1.15) translateY(-2px); }
      100% { transform: scale(1); }
    }

    .micro-tilt_head { animation: micro-tilt 0.6s ease-in-out; }
    @keyframes micro-tilt {
      0%, 100% { transform: rotate(0deg); }
      50% { transform: rotate(5deg); }
    }

    .micro-hesitate { animation: micro-hesitate 0.8s ease-in-out; }
    @keyframes micro-hesitate {
      0%, 100% { opacity: 1; transform: translateX(0); }
      25% { opacity: 0.6; transform: translateX(-1px); }
      75% { opacity: 0.8; transform: translateX(1px); }
    }

    .micro-sway { animation: micro-sway 1.2s ease-in-out; }
    @keyframes micro-sway {
      0%, 100% { transform: rotate(0deg); }
      25% { transform: rotate(3deg); }
      75% { transform: rotate(-3deg); }
    }

    .micro-settle { animation: micro-settle 0.5s ease-out; }
    @keyframes micro-settle {
      0% { transform: translateY(-1px); }
      100% { transform: translateY(0); }
    }

    .micro-nod { animation: micro-nod 0.4s ease-in-out; }
    @keyframes micro-nod {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(2px); }
    }

    .micro-turn_away { animation: micro-turn 0.6s ease-in-out; }
    @keyframes micro-turn {
      0% { transform: scaleX(1); }
      50% { transform: scaleX(-0.9); }
      100% { transform: scaleX(1); }
    }

    .micro-shrink { animation: micro-shrink 0.5s ease-out; }
    @keyframes micro-shrink {
      0% { transform: scale(1); }
      50% { transform: scale(0.9); }
      100% { transform: scale(1); }
    }

    .micro-perk_up { animation: micro-perk 0.4s ease-out; }
    @keyframes micro-perk {
      0% { transform: scale(1) translateY(0); }
      50% { transform: scale(1.05) translateY(-3px); }
      100% { transform: scale(1) translateY(0); }
    }

    /* Combo animations */
    .micro-hesitate_then_burst { animation: micro-h-burst 1.5s ease-in-out; }
    @keyframes micro-h-burst {
      0%, 20% { opacity: 0.5; transform: scale(0.95); }
      40% { opacity: 0.8; }
      60% { opacity: 1; transform: scale(1.1) translateY(-3px); }
      100% { transform: scale(1); }
    }

    .micro-shock_confused { animation: micro-shock 0.8s ease-in-out; }
    @keyframes micro-shock {
      0% { transform: scale(1.1) rotate(-3deg); }
      50% { transform: scale(1.05) rotate(3deg); }
      100% { transform: scale(1) rotate(0); }
    }

    .micro-shy_drift { animation: micro-shy 1.2s ease-in-out; }
    @keyframes micro-shy {
      0%, 100% { transform: translateX(0) rotate(0); opacity: 1; }
      30% { transform: translateX(-2px) rotate(-2deg); opacity: 0.7; }
      70% { transform: translateX(2px) rotate(2deg); opacity: 0.8; }
    }

    .micro-quiet_acceptance { animation: micro-quiet 0.8s ease-out; }
    @keyframes micro-quiet {
      0% { transform: scale(1.02); opacity: 0.9; }
      100% { transform: scale(1); opacity: 1; }
    }

    .micro-excited_sway { animation: micro-excited 1s ease-in-out; }
    @keyframes micro-excited {
      0%, 100% { transform: translateY(0) rotate(0); }
      25% { transform: translateY(-3px) rotate(3deg); }
      75% { transform: translateY(-1px) rotate(-2deg); }
    }

    /* Mood-based adjustments */
    .mood-bounce { animation: mood-bounce 2s ease-in-out infinite; }
    @keyframes mood-bounce {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(-2px); }
    }

    .mood-shake { animation: mood-shake 0.3s ease-in-out infinite; }
    @keyframes mood-shake {
      0%, 100% { transform: translateX(0); }
      25% { transform: translateX(-1px); }
      75% { transform: translateX(1px); }
    }

    .mood-shrink { transform: scale(0.9); }

    /* Speed adjustment via CSS variable */
    #pet-container {
      --mood-speed: 1.0;
      --mood-glow: 0.3;
    }
  `;
  document.head.appendChild(style);
}

export default {
  initMicroAnimations,
  setMood,
  onToken,
  resetBuffer,
  injectMicroAnimationStyles,
};
