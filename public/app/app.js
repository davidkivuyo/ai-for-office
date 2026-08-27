/**
 * Nexus.ai — frontend wired to FastAPI backend per AGENTS Phase 1.
 * All backend touchpoints go through `api` helpers; UI never talks to Ollama directly.
 */

const API_BASE = ""; // same origin; FastAPI serves /api/*

/* ---------------- Token & helpers ---------------- */
const TOKEN_KEY = "nexus_token";

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}
function authHeaders() {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiFetch(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...authHeaders(), ...(opts.headers || {}) };
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (!res.ok) {
    const body = await res.text();
    let msg = body;
    try {
      const j = JSON.parse(body);
      msg = j.detail || j.message || body;
    } catch (_) {}
    const err = new Error(msg || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

/* ---------------- API objects ---------------- */
const api = {
  async login(username, password) {
    const data = await apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    return data;
  },
  async register(username, password, display_name) {
    const data = await apiFetch("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name: display_name || username }),
    });
    return data;
  },
  async me() {
    return apiFetch("/api/auth/me");
  },
  async listConversations() {
    return apiFetch("/api/conversations");
  },
  async createConversation(title) {
    return apiFetch("/api/conversations", { method: "POST", body: JSON.stringify({ title }) });
  },
  async getConversation(id) {
    return apiFetch(`/api/conversations/${id}`);
  },
  async sendMessage(conversationId, text, nodeId) {
    const body = { message: text, stream: false };
    if (conversationId) body.conversation_id = conversationId;
    if (nodeId) body.node_id = nodeId;
    return apiFetch("/api/chat", { method: "POST", body: JSON.stringify(body) });
  },
  async sendMessageStream(conversationId, text, nodeId, onToken) {
    const body = { message: text, stream: true };
    if (conversationId) body.conversation_id = conversationId;
    if (nodeId) body.node_id = nodeId;
    const res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ ...body, stream: true }),
    });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalMeta = null;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const data = trimmed.slice(5).trim();
          if (data === "[DONE]") return finalMeta;
          try {
            const obj = JSON.parse(data);
            if (obj.token) onToken(obj.token);
            if (obj.done) finalMeta = obj;
            if (obj.error) throw new Error(obj.error);
          } catch (e) {
            if (e instanceof SyntaxError) continue;
            throw e;
          }
        }
      }
    } finally {
      await reader.cancel();
    }
    return finalMeta;
  },
  async nodesHealth() {
    return apiFetch("/api/nodes/health");
  },
};

/* ---------------- State ---------------- */
let conversations = [];
let activeConversation = null;
let messagesCache = []; // current conv messages
let healthPoll = null;

const $ = (s) => document.querySelector(s);
const stream = $("#stream");
const listEl = $("#conversation-list");
const inputEl = $("#composer-input");
const latencyHint = $("#latency-hint");

function el(tag, className, html) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (html !== undefined) n.innerHTML = html;
  return n;
}

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

/* ---------------- Rendering ---------------- */
function renderConversations() {
  listEl.innerHTML = "";
  conversations.forEach((conv) => {
    const li = el("li", conv.id === activeConversation ? "active" : "", escapeHtml(conv.title));
    li.addEventListener("click", async () => {
      activeConversation = conv.id;
      $("#project-title").textContent = conv.title;
      renderConversations();
      await loadMessages(conv.id);
    });
    listEl.appendChild(li);
  });
}

function renderMessage(msg, meta, isTyping) {
  const wrapper = el("div", `msg ${msg.role}`);
  if (msg.role === "assistant") {
    wrapper.appendChild(el("div", "msg-mark"));
    const body = el("div", "msg-body");
    const p = el("p");
    const content = msg.content || msg.text || "";
    if (isTyping) {
      p.innerHTML = content;
    } else {
      p.textContent = content;
    }
    body.appendChild(p);
    if (meta) {
      const m = el("div", "msg-meta");
      m.textContent = `${meta.actual_model || meta.model || ""} · ${meta.node_id || meta.actual_node || ""} · ${meta.latency_ms ? meta.latency_ms + "ms" : ""}`;
      body.appendChild(m);
    }
    wrapper.appendChild(body);
  } else {
    const bubble = el("div", "bubble");
    const p = el("p");
    p.textContent = msg.content || msg.text || "";
    bubble.appendChild(p);
    wrapper.appendChild(bubble);
  }
  stream.appendChild(wrapper);
  stream.scrollTop = stream.scrollHeight;
  return wrapper;
}

function clearStream() {
  stream.innerHTML = "";
}

function showTyping() {
  return renderMessage({ role: "assistant", content: '<span class="typing"><span></span><span></span><span></span></span>' }, null, true);
}

/* ---------------- Auth UI ---------------- */
const authOverlay = $("#auth-overlay");
const authForm = $("#auth-form");
const regForm = $("#register-form");

function showAuth(msg) {
  authOverlay.hidden = false;
  const errEl = $("#auth-error");
  if (msg) {
    errEl.textContent = msg;
    errEl.hidden = false;
  } else {
    errEl.textContent = "";
    errEl.hidden = true;
  }
}
function hideAuth() {
  authOverlay.hidden = true;
}
async function checkAuth() {
  const tok = getToken();
  if (!tok) {
    showAuth();
    return false;
  }
  try {
    const user = await api.me();
    onAuthed(user);
    return true;
  } catch (e) {
    if (e.status === 401) {
      setToken(null);
      showAuth("Session expired — please sign in");
      return false;
    }
    // Backend not reachable — still show app but with error hint
    console.warn("me failed", e);
    return false;
  }
}
function onAuthed(user) {
  hideAuth();
  $("#user-name").textContent = user.display_name || user.username;
  $("#avatar").textContent = (user.display_name || user.username || "?").slice(0, 2).toUpperCase();
  // after auth, load data
  refreshConversations();
  refreshNodes();
  startHealthPoll();
}

authForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#auth-error").hidden = true;
  const u = $("#auth-username").value.trim();
  const p = $("#auth-password").value;
  try {
    const data = await api.login(u, p);
    setToken(data.access_token);
    onAuthed(data.user);
  } catch (err) {
    const el = $("#auth-error");
    el.textContent = err.message || "Login failed";
    el.hidden = false;
  }
});
regForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#reg-error").hidden = true;
  const u = $("#reg-username").value.trim();
  const p = $("#reg-password").value;
  const d = $("#reg-display").value.trim();
  try {
    const data = await api.register(u, p, d);
    setToken(data.access_token);
    onAuthed(data.user);
  } catch (err) {
    const el = $("#reg-error");
    el.textContent = err.message || "Register failed";
    el.hidden = false;
  }
});
$("#logout")?.addEventListener("click", () => {
  setToken(null);
  conversations = [];
  activeConversation = null;
  clearStream();
  renderConversations();
  showAuth();
  stopHealthPoll();
});

/* ---------------- Data loading ---------------- */
async function refreshConversations() {
  try {
    const list = await api.listConversations();
    conversations = list;
    if (!activeConversation && conversations.length) {
      activeConversation = conversations[0].id;
      $("#project-title").textContent = conversations[0].title;
      await loadMessages(activeConversation);
    }
    renderConversations();
  } catch (e) {
    console.warn("listConversations failed", e);
    latencyHint.textContent = "Offline — backend not reachable";
  }
}
async function loadMessages(convId) {
  clearStream();
  try {
    const data = await api.getConversation(convId);
    messagesCache = data.messages || [];
    messagesCache.forEach((m) => renderMessage(m, { model: m.model, node_id: m.node_id, latency_ms: m.latency_ms }));
  } catch (e) {
    renderMessage({ role: "assistant", content: `Could not load messages: ${e.message ?? ""}` });
  }
}

async function refreshNodes() {
  try {
    const data = await api.nodesHealth();
    const nodes = data.nodes || [];
    // Update sidebar dots
    nodes.forEach((n) => {
      const dot = document.getElementById(`dot-${n.node_id}`);
      const label = document.getElementById(`label-${n.node_id}`);
      if (!dot || !label) return;
      dot.style.background = n.status === "healthy" ? "#22a35c" : n.status === "degraded" ? "#d9a021" : n.status === "disabled" ? "#9ca3af" : "#b02718";
      label.textContent = n.model ? `${n.node_id} — ${n.model} · ${n.status}` : `${n.node_id} — ${n.status}`;
    });
    // Update select labels if we have model info
    const sel = $("#node-select");
    if (sel && nodes.length) {
      Array.from(sel.options).forEach((opt) => {
        if (!opt.value) return;
        const n = nodes.find((x) => x.node_id === opt.value);
        if (n) opt.textContent = n.model ? `${n.node_id} — ${n.model} (${n.status})` : `${n.node_id} (${n.status})`;
      });
    }
  } catch (e) {
    console.warn("nodesHealth failed", e);
  }
}
function startHealthPoll() {
  if (healthPoll) return;
  healthPoll = setInterval(refreshNodes, 30000);
}
function stopHealthPoll() {
  if (healthPoll) {
    clearInterval(healthPoll);
    healthPoll = null;
  }
}
$("#node-health-btn")?.addEventListener("click", refreshNodes);

/* ---------------- Compose ---------------- */
async function submit(rawText) {
  const text = (rawText ?? inputEl.value).trim();
  if (!text) return;
  const nodeId = $("#node-select")?.value || null;
  const useStream = $("#stream-toggle")?.checked;
  renderMessage({ role: "user", content: text });
  inputEl.value = "";
  const pending = showTyping();

  try {
    if (useStream) {
      // Streaming: replace pending with live token accumulation
      const bodyEl = pending.querySelector(".msg-body p");
      bodyEl.textContent = "";
      let full = "";
      const meta = await api.sendMessageStream(activeConversation, text, nodeId, (tok) => {
        full += tok;
        bodyEl.textContent = full;
        stream.scrollTop = stream.scrollHeight;
      });
      pending.remove();
      const replyMeta = meta || {};
      renderMessage({ role: "assistant", content: full || "(empty response)" }, replyMeta);
      if (replyMeta.conversation_id && !activeConversation) {
        activeConversation = replyMeta.conversation_id;
      }
      if (replyMeta.latency_ms) latencyHint.textContent = `${replyMeta.actual_model || ""} · ${replyMeta.latency_ms}ms`;
      // Refresh list if this was a new conversation
      await refreshConversations();
      if (activeConversation) {
        // Ensure we stay on this conv; reload messages to sync
        // Avoid duplicate render if we already did — just keep as is
      }
    } else {
      const res = await api.sendMessage(activeConversation, text, nodeId);
      pending.remove();
      if (!activeConversation) {
        activeConversation = res.conversation_id;
        $("#project-title").textContent = conversations.find((c) => c.id === activeConversation)?.title || res.conversation_id;
      }
      renderMessage({ role: "assistant", content: res.reply }, res);
      latencyHint.textContent = `${res.actual_model} · ${res.latency_ms}ms`;
      await refreshConversations();
    }
  } catch (err) {
    pending.remove();
    const isNetwork = err.message?.includes("Failed to fetch") || err.message?.includes("NetworkError");
    const detail = isNetwork ? "Backend not reachable. Is the FastAPI server running on :8000?" : (err.message || "Unknown error");
    renderMessage({ role: "assistant", content: `Error: ${detail}` });
  }
}

function initCompose() {
  $("#send")?.addEventListener("click", () => submit());
  inputEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  $("#new-conversation")?.addEventListener("click", async () => {
    try {
      const conv = await api.createConversation("Untitled");
      conversations.unshift(conv);
      activeConversation = conv.id;
      $("#project-title").textContent = conv.title;
      clearStream();
      renderConversations();
    } catch (e) {
      // Fallback: local draft only. Let the backend create the conversation
      // on the first message so the id stays server-owned.
      activeConversation = null;
      $("#project-title").textContent = "Untitled Document (unsaved)";
      clearStream();
      renderConversations();
    }
  });
}

/* ---------------- Init ---------------- */
async function init() {
  initCompose();
  const ok = await checkAuth();
  if (!ok) {
    renderConversations();
  }
}
init();
