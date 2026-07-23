"use strict";

(function initSettingsTabEmotion(root) {
  let core = null;
  let helpers = null;
  let mounted = false;

  function t(key) { return helpers.t(key); }

  // Known emotion tags that the animation system supports
  const EMOTIONS = ["happy", "curious", "sad", "excited", "mad", "neutral"];

  // Human-readable labels for emotions (fallback if i18n is missing)
  const EMOTION_LABELS = {
    happy: "开心", curious: "好奇", sad: "难过",
    excited: "兴奋", mad: "生气", neutral: "平静"
  };

  // Emoji icons per emotion for quick visual scan
  const EMOTION_ICONS = {
    happy: "😸", curious: "🤔", sad: "😿",
    excited: "🙌", mad: "😤", neutral: "😐"
  };

  function cleanupTimers() { mounted = false; }

  // Read the current theme's attention state bindings and extract
  // emotion → file mappings
  function getEmotionMap() {
    try {
      const theme = core.state.theme;
      if (!theme || !theme.states || !theme.states.attention) return [];
      const attentionFiles = Array.isArray(theme.states.attention) ? theme.states.attention : [];
      const map = [];
      const seen = new Set();
      // Collect emotion-tagged variants
      for (const entry of attentionFiles) {
        if (typeof entry === "object" && entry.file && entry.emotion) {
          const emo = entry.emotion.toLowerCase();
          if (!seen.has(emo)) {
            seen.add(emo);
            map.push({ emotion: emo, file: entry.file });
          }
        } else if (typeof entry === "string") {
          // Plain strings count as "neutral" or un-tagged default
          if (!seen.has("neutral")) {
            seen.add("neutral");
            map.push({ emotion: "neutral", file: entry, isDefault: true });
          }
        }
      }
      return map;
    } catch { return []; }
  }

  function renderHeader(parent) {
    parent.appendChild(_el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      _el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, "情绪动画映射"),
      _el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } },
        "聊天时桌宠会根据回复末尾的 [EMOTION:xxx] 标签自动切换到对应的动画。以下是当前主题的情绪映射表。"),
    ));
  }

  function _el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "className") e.className = v;
        else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
        else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v !== undefined && v !== null) e.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (c == null) continue;
      if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
      else if (c instanceof Node) e.appendChild(c);
    }
    return e;
  }

  function renderContent(parent) {
    const emotMap = getEmotionMap();
    if (emotMap.length === 0) {
      parent.appendChild(_el("p", { style: { color: "var(--text-secondary)", fontSize: "13px", fontStyle: "italic", padding: "16px 0" } },
        "当前主题没有配置情绪动画映射。在主题的 theme.json 中，给 states.attention 数组添加 { \"file\": \"动画.gif\", \"emotion\": \"happy\" } 格式的条目即可。"));
      return;
    }

    const table = _el("div", { style: { display: "flex", flexDirection: "column", gap: "8px", marginTop: "16px" } });

    for (const item of emotMap) {
      const icon = EMOTION_ICONS[item.emotion] || "🎬";
      const label = EMOTION_LABELS[item.emotion] || item.emotion;
      const row = _el("div", {
        style: {
          display: "flex", alignItems: "center", gap: "12px",
          padding: "10px 14px", borderRadius: "8px",
          background: "var(--panel-bg)", border: "1px solid var(--border)",
        }
      });

      // Emotion icon + label
      const left = _el("div", { style: { display: "flex", alignItems: "center", gap: "8px", minWidth: "120px" } });
      left.appendChild(_el("span", { style: { fontSize: "22px" } }, icon));
      left.appendChild(_el("span", { style: { fontSize: "14px", fontWeight: "600", color: "var(--text-primary)" } }, label));

      // Tag pill
      const tag = _el("span", {
        style: {
          fontSize: "11px", fontFamily: "monospace",
          background: "color-mix(in srgb, var(--accent) 15%, transparent)",
          color: "var(--accent)", padding: "2px 8px", borderRadius: "10px",
        }
      }, "[EMOTION:" + item.emotion + "]");
      left.appendChild(tag);

      // File name
      const file = _el("span", {
        style: { fontFamily: "monospace", fontSize: "12px", color: "var(--text-secondary)", flex: "1" }
      }, item.file);

      row.appendChild(left);
      row.appendChild(file);

      // Preview hint
      if (item.isDefault) {
        row.appendChild(_el("span", {
          style: { fontSize: "11px", color: "var(--text-secondary)", fontStyle: "italic" }
        }, "默认"));
      }

      table.appendChild(row);
    }

    parent.appendChild(table);

    // Add note about how to extend
    parent.appendChild(_el("div", {
      style: { marginTop: "20px", padding: "12px", borderRadius: "6px", background: "color-mix(in srgb, var(--accent) 8%, transparent)", border: "1px solid var(--border)", color: "var(--text-primary)" }
    },
      _el("p", { style: { margin: "0 0 6px 0", fontSize: "13px", fontWeight: "600" } }, "如何添加自定义情绪？"),
      _el("p", { style: { margin: "0", fontSize: "12px", color: "var(--text-secondary)" } },
        "打开主题文件夹的 theme.json，在 states.attention 数组中添加 {\"file\": \"你的动画.gif\", \"emotion\": \"想要的标签\"}。系统提示词已经教模型使用 happy/curious/sad/excited/mad/neutral 六种标签，模型也可以自由创造新的情绪词——只要在主题里添加对应的映射就能显示。" +
        "\n\n模型回复时输出的 [EMOTION:xxx] 标签会被自动解析，桌宠切换到对应的动画。如果找不到匹配的变体，会回退到默认动画。")
    ));
  }

  function render(parent) {
    cleanupTimers();
    mounted = true;
    parent.innerHTML = "";
    renderHeader(parent);
    renderContent(parent);
  }

  function init(coreArg) {
    core = coreArg; helpers = core.helpers;
    core.tabs.emotion = { render: (parent) => { void render(parent); } };
  }

  root.ClawdSettingsTabEmotion = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
