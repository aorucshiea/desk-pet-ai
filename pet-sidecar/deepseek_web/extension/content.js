// Content script: run one bridge job on the DeepSeek web chat page.
(() => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  document.documentElement.setAttribute("data-ds-bridge", "1");
  document.documentElement.setAttribute("data-ds-bridge-version", "0.4.2");
  console.log("[DeepSeekBridge] content script loaded v0.4.2");

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  }

  function findInput() {
    for (const selector of ["textarea#chat-input", "textarea"]) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el) && !el.disabled) return el;
      }
    }
    return null;
  }

  function fillInput(el, text) {
    el.focus();
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      const proto = el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.textContent = text;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
    }
  }

  async function clickSend(input) {
    const candidates = [
      'div[role="button"][aria-label*="发送"]',
      'button[aria-label*="发送"]',
      'div[role="button"][aria-label*="Send"]',
      'button[aria-label*="Send"]',
      'div[class*="ds-button--primary"]',
      'button[class*="ds-button--primary"]',
      'button[type="submit"]',
      'button[class*="send"]',
    ];
    // The composer re-renders while the image attachment finishes uploading;
    // the send button can appear late and be disabled for a few seconds.
    for (let attempt = 0; attempt < 30; attempt += 1) {
      for (const selector of candidates) {
        for (const el of document.querySelectorAll(selector)) {
          if (!visible(el)) continue;
          if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
          el.click();
          return true;
        }
      }
      await sleep(200);
    }
    return false;
  }

  function pressEnter(input) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      input.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
      }));
    }
  }

  function isGenerating() {
    const nodes = document.querySelectorAll(
      'div[role="button"], button, [class*="stop"], [class*="Stop"]'
    );
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = String(el.getAttribute("aria-label") || "") + " " + String(el.innerText || "");
      if (/stop|停止|中止/i.test(label)) return true;
    }
    return false;
  }

  function assistantTexts() {
    const selectors = [".ds-markdown.ds-markdown--block", ".ds-markdown"];
    for (const selector of selectors) {
      const texts = [];
      for (const el of document.querySelectorAll(selector)) {
        // Never return the model's hidden think/reasoning block as the reply.
        // The block itself is a .ds-markdown inside .ds-think-content, so
        // removing descendants is not enough — skip the whole subtree.
        if (el.closest(
          '[class*="think"], [class*="Think"], [class*="reasoning"], [class*="Reasoning"]'
        )) continue;
        const clone = el.cloneNode(true);
        clone.querySelectorAll(
          '[class*="think"], [class*="Think"], [class*="reasoning"], [class*="Reasoning"]'
        ).forEach((node) => node.remove());
        const text = (clone.innerText || clone.textContent || "").trim();
        if (text) texts.push(text);
      }
      if (texts.length) return texts;
    }
    return [];
  }

  function findNewChat() {
    const nodes = document.querySelectorAll('button, div[role="button"], a, [tabindex="0"]');
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = String(el.innerText || "") + " " + String(el.getAttribute("aria-label") || "");
      if (/^(new chat|新对话|新建对话)$/i.test(label.trim()) || /new chat|新对话|新建对话/i.test(label)) {
        return el;
      }
    }
    // Collapsed sidebar: "New chat" is a zero-size span inside a tabindex
    // div with no accessible name. Text match is the only reliable signal,
    // and `.click()` still reaches React's handler on the hidden ancestor.
    for (const el of document.querySelectorAll("span")) {
      const text = String(el.textContent || "").trim();
      if (!/^(new chat|新对话|新建对话)$/i.test(text)) continue;
      const clickable = el.closest('[tabindex="0"], [role="button"], button, a');
      if (clickable) return clickable;
    }
    return null;
  }

  async function maybeNewChat() {
    const button = findNewChat();
    if (!button) return;
    const before = location.href;
    button.click();
    // SPA routing takes a moment; a fixed 700ms sometimes left the old
    // conversation in place, and the full prompt was appended to it.
    const started = Date.now();
    while (Date.now() - started < 2500) {
      await sleep(100);
      if (location.href !== before) break;
      if (!document.querySelector(".ds-markdown")) break;
    }
    await sleep(300);
  }

  async function waitForReply(baseline) {
    const started = Date.now();
    let last = "";
    let stableSince = 0;
    let sawChange = false;

    while (Date.now() - started < 240_000) {
      await sleep(300);
      if (location.pathname.includes("/sign_in")) {
        throw new Error("DeepSeek 页面未登录，请先在浏览器里登录");
      }
      const texts = assistantTexts();
      const current = texts.length ? texts[texts.length - 1] : "";
      if (current && current !== baseline) sawChange = true;

      if (sawChange) {
        if (current !== last) {
          last = current;
          stableSince = Date.now();
        } else if (!isGenerating() && Date.now() - stableSince > 1200) {
          return current;
        }
      }

      if (!sawChange && !isGenerating() && Date.now() - started > 25_000) {
        throw new Error("页面没有产生新回复（可能没有发送成功）");
      }
    }
    throw new Error("等待网页回复超时（240s）");
  }

  // ------------------------------------------------------------------
  // Image delivery: base64 -> File -> page upload input / paste / drop.
  // ------------------------------------------------------------------
  const MAX_ATTACH_IMAGES = 4;
  const PREVIEW_SELECTOR = [
    'img[src^="blob:"]',
    'img[src^="data:"]',
    '[class*="image"]',
    '[class*="attach"]',
    '[class*="file"]',
  ].join(", ");

  function base64ToFile(image) {
    const raw = String((image && image.base64) || "").replace(/\s+/g, "");
    const mime = String((image && image.mime) || "image/jpeg");
    if (!raw) throw new Error("图片 base64 为空");
    const binary = atob(raw);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    // The page keys off File.type; the name is only cosmetic.
    return new File([bytes], "screenshot.jpg", { type: mime });
  }

  function imagesToFiles(images) {
    const files = [];
    for (const image of (Array.isArray(images) ? images : []).slice(0, MAX_ATTACH_IMAGES)) {
      try {
        files.push(base64ToFile(image));
      } catch (err) {
        console.warn("[DeepSeekBridge] skip invalid image:", err);
      }
    }
    if (!files.length) throw new Error("图片数据无效，无法转换为文件");
    return files;
  }

  function findFileInput() {
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
    if (!inputs.length) return null;
    // Prefer an input that explicitly accepts images; hidden is fine.
    const imageInput = inputs.find((el) =>
      /image/i.test(String(el.getAttribute("accept") || ""))
    );
    return imageInput || inputs[0];
  }

  function countPreviews() {
    let count = 0;
    for (const el of document.querySelectorAll(PREVIEW_SELECTOR)) {
      if (visible(el)) count += 1;
    }
    return count;
  }

  async function waitForPreview(baseline, timeoutMs) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      if (countPreviews() > baseline) return true;
      await sleep(200);
    }
    return countPreviews() > baseline;
  }

  async function waitForUploadDone(timeoutMs = 10000) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const spinner = Array.from(document.querySelectorAll(".ds-loading")).some(visible);
      const input = findFileInput();
      const files = input && input.files ? input.files.length : 0;
      // The site clears input.files as soon as it has consumed the file;
      // the loading spinner disappears when upload/preview parsing is done.
      if (!spinner && files === 0 && Date.now() - started > 300) return true;
      await sleep(200);
    }
    return false;
  }

  // DeepSeek's page displays the raw assistant text, including the pet's
  // hidden [INNER:]/MEM/DSTATE blocks. Those are protocol metadata, not
  // something the user should read; strip them from the visible DOM after
  // the bridge has already captured the raw text.
  function stripInternalMarkup(text) {
    return String(text || "")
      .replace(/<<<MEMEND>>>/gi, "")
      .replace(/<<<DSTATEEND>>>/gi, "")
      // The page can truncate a long reply before its closing marker; treat
      // an unterminated MEM/DSTATE block as running to the end of the text.
      .replace(/<<<MEM>>>[\s\S]*?(?:<<<MEMEND>>>|$)/gi, "")
      .replace(/<<<DSTATE>>>[\s\S]*?(?:<<<DSTATEEND>>>|$)/gi, "")
      .replace(/\[INNER:[\s\S]*?(?:\]|$)/gi, "")
      .replace(/\[EMOTION:[^\]]*\]/gi, "")
      .replace(/\[NEXT_CHAT:[^\]]*\]/gi, "")
      .replace(/\[WALK(?:_DESKTOP)?[^\]]*\]/gi, "")
      .replace(/\[MCP:[^\]]*\]/gi, "")
      .replace(/_执行[^\n]*_\n?/g, "")
      .trim();
  }

  function sanitizeNode(el) {
    if (!el || !el.isConnected) return;
    const raw = (el.innerText || el.textContent || "").trim();
    if (!/<<<MEM|<<<DSTATE|\[INNER:|\[EMOTION:|\[NEXT_CHAT:|\[MCP:|_执行/.test(raw)) {
      return;  // ordinary reply: leave its formatting alone
    }
    const cleaned = stripInternalMarkup(raw);
    const onlyParenthetical = /^[（(][\s\S]*[）)]$/.test(cleaned);
    const onlyMarkup = /^[<>_\s]*$/.test(cleaned);
    if (!cleaned || onlyParenthetical || onlyMarkup) {
      el.remove();
      return;
    }
    if (cleaned !== raw) {
      el.textContent = cleaned;
    }
  }

  // Clean replies left over from a previous content-script version / page
  // load. Never touch a message while the page is still generating: the
  // bridge needs the raw text (including MEM/DSTATE) intact until it has
  // read the completed reply.
  function sanitizeExisting() {
    if (isGenerating()) return;
    const nodes = Array.from(document.querySelectorAll(".ds-markdown"))
      .filter((el) => !el.closest(
        '[class*="think"], [class*="Think"], [class*="reasoning"], [class*="Reasoning"]'
      ));
    for (const el of nodes) sanitizeNode(el);
  }

  function makeDataTransfer(files) {
    const data = new DataTransfer();
    for (const file of files) data.items.add(file);
    return data;
  }

  function attachViaFileInput(fileInput, files) {
    const data = makeDataTransfer(files);
    try {
      fileInput.files = data.files;
    } catch (err) {
      console.warn("[DeepSeekBridge] assigning input.files failed:", err);
      return false;
    }
    fileInput.dispatchEvent(new Event("input", { bubbles: true }));
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function attachViaPaste(target, files) {
    const data = makeDataTransfer(files);
    let event;
    try {
      event = new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: data,
      });
    } catch (err) {
      event = new Event("paste", { bubbles: true, cancelable: true });
    }
    if (!event.clipboardData) {
      try {
        Object.defineProperty(event, "clipboardData", { value: data });
      } catch (err) {}
    }
    const node = target || document.activeElement || document.body;
    node.dispatchEvent(event);
  }

  function attachViaDrop(target, files) {
    const data = makeDataTransfer(files);
    const node = target || document.body;
    for (const type of ["dragenter", "dragover", "drop"]) {
      node.dispatchEvent(new DragEvent(type, {
        bubbles: true,
        cancelable: true,
        dataTransfer: data,
      }));
    }
  }

  async function attachImages(images, input) {
    const files = imagesToFiles(images);
    const baseline = countPreviews();
    const fileInput = findFileInput();

    if (fileInput && attachViaFileInput(fileInput, files)) {
      // Never click an upload button: that can open a native file dialog.
      const preview = await waitForPreview(baseline, 8000);
      console.log(
        "[DeepSeekBridge] attach strategy=file-input files=" + files.length +
        " preview=" + preview
      );
      return;
    }

    const composer = (input && (input.closest("form") || input)) ||
      document.activeElement || document.body;

    // The paste handler usually lives on the focused composer.
    if (input && typeof input.focus === "function") input.focus();
    attachViaPaste(input || composer, files);
    if (await waitForPreview(baseline, 2500)) {
      console.log("[DeepSeekBridge] attach strategy=paste files=" + files.length + " preview=true");
      return;
    }

    attachViaDrop(composer, files);
    if (await waitForPreview(baseline, 2500)) {
      console.log("[DeepSeekBridge] attach strategy=drop files=" + files.length + " preview=true");
      return;
    }

    console.warn("[DeepSeekBridge] attach failed: file-input missing, paste/drop produced no preview");
    throw new Error("找不到图片上传入口（DeepSeek 页面可能改版）");
  }

  async function runJob(job) {
    const id = job && job.id;
    try {
      if (!id) throw new Error("invalid job");
      if (location.pathname.includes("/sign_in")) {
        throw new Error("DeepSeek 页面未登录");
      }
      if (job.new_chat !== false) {
        await maybeNewChat();
      }
      let input = findInput();
      if (!input) throw new Error("找不到输入框（页面可能改版）");

      const before = assistantTexts();
      const baseline = before.length ? before[before.length - 1] : "";
      const promptText = String(job.prompt || "");
      // Fill FIRST: setting input.files makes the composer re-render and can
      // clear a textarea that was filled before the image attachment.
      fillInput(input, promptText);
      await sleep(200);

      if (Array.isArray(job.images) && job.images.length) {
        await attachImages(job.images, input);
        // Wait for the upload spinner to clear instead of always sleeping 3s.
        if (!await waitForUploadDone(10000)) {
          console.warn("[DeepSeekBridge] image upload did not settle within 10s");
        }
        const refreshed = findInput();
        if (!refreshed) throw new Error("图片上传后找不到输入框（页面可能改版）");
        input = refreshed;
        if (!String(input.value || "").trim()) {
          fillInput(input, promptText);
          await sleep(200);
        }
      }

      if (!await clickSend(input)) {
        pressEnter(input);
      }

      const text = await waitForReply(baseline);
      // Clean every raw protocol reply on the page, not just the one we
      // just read: history loaded from before a content-script reload can
      // still contain MEM/INNER text that the user should never see.
      sanitizeExisting();
      chrome.runtime.sendMessage({ type: "result", id, text, error: "", page: location.href });
    } catch (err) {
      chrome.runtime.sendMessage({
        type: "result",
        id,
        text: "",
        error: String((err && err.message) || err),
        page: location.href,
      });
    }
  }

  let running = false;
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "ping") {
      sendResponse({ ok: true, version: "0.4.2", running });
      return;
    }
    if (!msg || msg.type !== "run-job" || running) return;
    running = true;
    runJob(msg.job).finally(() => { running = false; });
  });

  setInterval(() => {
    try { chrome.runtime.sendMessage({ type: "poll" }); } catch (err) {}
  }, 1000);

  // Existing raw MEM/INNER replies (from before this script loaded) are
  // cleaned once the page is idle. DeepSeek hydrates the transcript late,
  // so retry a few times; each pass skips while a reply is generating.
  for (const delay of [1500, 4000, 8000, 15000]) {
    setTimeout(() => {
      try { sanitizeExisting(); } catch (err) {}
    }, delay);
  }
})();
