// Background: poll the local bridge and hand jobs to the DeepSeek tab.
const API = "http://127.0.0.1:8766";
let polling = false;

async function poll() {
  if (polling) return;
  polling = true;
  try {
    // Resolve the target tab before the long-poll so the bridge can key its
    // same-conversation session on the page URL (new chat / reload resets it).
    const tabs = await chrome.tabs.query({});
    const candidates = (tabs || []).filter((t) => {
      if (!t || !t.url) return false;
      return (t.url.includes("chat.deepseek.com") && !t.url.includes("/sign_in"))
        || t.url.includes("127.0.0.1:8766/mock");
    });
    // Prefer the tab the user can actually see. Chrome can keep a hidden
    // duplicate/prerender of the same chat URL; sending jobs there makes the
    // visible conversation look frozen while the model answers invisibly.
    const tab = candidates.find((t) => t.active) || candidates[0];
    if (!tab || !tab.id) return;
    const page = encodeURIComponent(tab.url || "");
    const resp = await fetch(API + "/jobs/next?page=" + page);
    if (resp.status !== 200) return;
    const job = await resp.json();
    const message = { type: "run-job", job };
    try {
      await chrome.tabs.sendMessage(tab.id, message);
    } catch (err) {
      // Reloading the extension orphans content scripts in already-open
      // tabs. Inject it once and retry before giving up; otherwise the job
      // sits in the bridge queue until the 240s timeout and the pet looks
      // frozen after a tool call.
      console.warn("[DeepSeekBridge] tab not ready, injecting content script:", err);
      try {
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
        await new Promise((resolve) => setTimeout(resolve, 300));
        await chrome.tabs.sendMessage(tab.id, message);
      } catch (retryErr) {
        console.error("[DeepSeekBridge] tab still not ready:", retryErr);
        await postResult(job.id, "", "标签页没有响应（内容脚本未加载），请刷新 DeepSeek 标签页", tab.url);
      }
    }
  } catch (err) {
    // Bridge not running: stay quiet and retry on the next poll.
  } finally {
    polling = false;
  }
}

async function postResult(id, text, error, page) {
  try {
    await fetch(API + "/jobs/" + encodeURIComponent(id) + "/result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text || "", error: error || "", page: page || "" }),
    });
  } catch (err) {
    console.warn("[DeepSeekBridge] failed to post result:", err);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "poll") {
    poll();
    sendResponse({ ok: true });
    return;
  }
  if (msg.type === "result") {
    postResult(msg.id, msg.text, msg.error, msg.page);
    sendResponse({ ok: true });
  }
});
