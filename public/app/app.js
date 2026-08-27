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
  if (res.status === 204) return null;
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
  async getConversation(id, opts = {}) {
    return apiFetch(`/api/conversations/${id}`, opts);
  },
  async sendMessage(conversationId, text, nodeId, opts = {}) {
    const body = { message: text, stream: false };
    if (conversationId) body.conversation_id = conversationId;
    if (nodeId) body.node_id = nodeId;
    return apiFetch("/api/chat", { method: "POST", body: JSON.stringify(body), signal: opts.signal });
  },
  async sendMessageStream(conversationId, text, nodeId, onToken, opts = {}) {
    const body = { message: text, stream: true };
    if (conversationId) body.conversation_id = conversationId;
    if (nodeId) body.node_id = nodeId;
    const res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ ...body, stream: true }),
      signal: opts.signal,
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
  async deleteConversation(id) {
    return apiFetch(`/api/conversations/${id}`, { method: "DELETE" });
  },
  async renameConversation(id, title) {
    return apiFetch(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
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
let loadMessagesGeneration = 0;
let loadMessagesAbortController = null;
let chatAbortController = null;
let isSending = false;
let chatGeneration = 0;

function abortCurrentChat() {
  if (chatAbortController) {
    try { chatAbortController.abort(); } catch (_) {}
  }
  // invalidate any pending submit generation so stale completions don't touch new conversation
  if (isSending || chatAbortController) {
    chatGeneration++;
  }
  chatAbortController = null;
  if (isSending) setSendingState(false);
}

function setSendingState(sending) {
  isSending = sending;
  const btn = $("#send");
  if (!btn) return;
  if (sending) {
    btn.textContent = "■";
    btn.setAttribute("aria-label", "Stop generation");
    btn.title = "Stop generation";
    btn.dataset.mode = "stop";
  } else {
    btn.textContent = "→";
    btn.setAttribute("aria-label", "Send message");
    btn.title = "Send message";
    btn.dataset.mode = "send";
  }
}

function isAbortError(err) {
  return err && (err.name === "AbortError" || /aborted|AbortError/i.test(err.message || ""));
}

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

function sanitizeUrl(url) {
  const raw = String(url || "").trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  if (lower.startsWith("javascript:") || lower.startsWith("data:") || lower.startsWith("vbscript:")) return null;
  if (lower.startsWith("http://") || lower.startsWith("https://") || lower.startsWith("mailto:")) return raw;
  if (raw.startsWith("/") || raw.startsWith("#")) return raw;
  // Block bare protocol-less or suspicious URLs containing spaces or control chars
  if (/[\s<>]/.test(raw)) return null;
  // Allow relative without protocol only if it looks like a path? treat as unsafe otherwise
  return null;
}

function decodeHtml(str) {
  return String(str ?? "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function restoreInlineCodes(str, inlineCodes) {
  return str.replace(/\u0000IC(\d+)\u0000/g, (_, idx) => {
    const code = inlineCodes[Number(idx)] ?? "";
    return `<code>${escapeHtml(code)}</code>`;
  });
}

function inlineFormat(escapedText, inlineCodes) {
  let s = escapedText;
  // Images: ![alt](url) — must run before link regex so ![alt](url) is not consumed as [alt](url)
  s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m, alt, url) => {
    const decodedUrl = decodeHtml(url);
    const safe = sanitizeUrl(decodedUrl);
    if (!safe) return escapeHtml(decodeHtml(alt));
    const decodedAlt = decodeHtml(alt);
    return `<img src="${escapeHtml(safe)}" alt="${escapeHtml(decodedAlt)}" loading="lazy" />`;
  });
  // Links: [text](url)
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) => {
    const decodedUrl = decodeHtml(url);
    const safe = sanitizeUrl(decodedUrl);
    if (!safe) return txt;
    return `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
  });
  // Autolink bare URLs (avoid already linked)
  s = s.replace(/(?<!["'>=])\bhttps?:\/\/[^\s<]+/g, (url) => {
    const decodedUrl = decodeHtml(url);
    const safe = sanitizeUrl(decodedUrl);
    if (!safe) return url;
    return `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener noreferrer">${escapeHtml(safe)}</a>`;
  });
  // Bold **text** and __text__
  s = s.replace(/\*\*([^\n*]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^\n_]+?)__/g, "<strong>$1</strong>");
  // Strikethrough ~~text~~
  s = s.replace(/~~([^\n~]+?)~~/g, "<del>$1</del>");
  // Italic *text* and _text_  (after bold so single markers remain)
  s = s.replace(/(?<!\*)\*([^\n*]+?)\*(?!\*)/g, "<em>$1</em>");
  // Use _italic_ only when surrounded by word boundaries or spaces to avoid breaking words_with_underscores
  s = s.replace(/(^|[^a-zA-Z0-9_])_([^\n_]+?)_([^a-zA-Z0-9_]|$)/g, (m, pre, content, post) => `${pre}<em>${content}</em>${post}`);
  // Inline code placeholders -> <code>
  s = restoreInlineCodes(s, inlineCodes);
  return s;
}

function renderMarkdown(md) {
  if (md == null) return "";
  const raw = String(md);
  if (!raw.trim()) return '<p class="rich-empty">—</p>';

  const codeBlocks = [];
  const inlineCodes = [];

  // Extract fenced code blocks ```lang\ncode```  (must be before inline code)
  let text = raw.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: (lang || "").trim().toLowerCase(), code: code });
    return `\u0000CB${idx}\u0000`;
  });

  // Extract inline code `code`
  text = text.replace(/`([^`\n]+?)`/g, (m, code) => {
    const idx = inlineCodes.length;
    inlineCodes.push(code);
    return `\u0000IC${idx}\u0000`;
  });

  // Escape the remaining text (preserves \u0000 placeholders)
  text = escapeHtml(text);

  const lines = text.split("\n");
  let html = "";
  let i = 0;

  const isHr = (l) => /^(---|\*\*\*|___)\s*$/.test(l.trim());
  const isHeading = (l) => /^(#{1,6})\s+(.*)$/.exec(l);
  const isUl = (l) => /^\s*[-*]\s+/.test(l);
  const isOl = (l) => /^\s*\d+\.\s+/.test(l);
  const isBlockquote = (l) => /^\s*(>|&gt;)\s?/.test(l);
  const isTableRow = (l) => l.includes("|") && l.includes("\u0000") === false && l.trim().length > 0;
  // Helper to detect table separator line like |---| --- | :---:
  const isTableSep = (l) => /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(l);

  while (i < lines.length) {
    let line = lines[i];

    // Skip blank lines (they separate blocks)
    if (!line.trim()) {
      i++;
      continue;
    }

    // Code block placeholder on its own line (could be inline with surrounding text? handle both)
    if (line.includes("\u0000CB")) {
      // If line is exactly a placeholder maybe with whitespace, restore as block
      // For simplicity, replace all placeholders in line
      const replaced = line.replace(/\u0000CB(\d+)\u0000/g, (_, idx) => {
        const cb = codeBlocks[Number(idx)];
        const escCode = escapeHtml(cb.code);
        const langCls = cb.lang ? ` class="language-${escapeHtml(cb.lang)}"` : "";
        const langLabel = cb.lang ? `<span class="code-lang">${escapeHtml(cb.lang)}</span>` : "";
        return `</p><pre><code${langCls}>${escCode}</code></pre><p>`;
        // We abuse p wrapping; will be cleaned. Instead emit directly:
      });
      // If the placeholder was the whole line, emit pre directly without p wrapper
      // Detect if line.trim() is exactly the placeholder
      if (/^\s*\u0000CB\d+\u0000\s*$/.test(line)) {
        const idx = Number(line.match(/\u0000CB(\d+)\u0000/)[1]);
        const cb = codeBlocks[idx];
        const escCode = escapeHtml(cb.code);
        const langCls = cb.lang ? ` class="language-${escapeHtml(cb.lang)}"` : "";
        const langLabel = cb.lang ? `<div class="code-head">${escapeHtml(cb.lang)}</div>` : "";
        html += `${langLabel}<pre><code${langCls}>${escCode}</code></pre>`;
      } else {
        // Inline-like code block inside paragraph — restore inline and treat as paragraph
        const inner = replaced.replace(/<\/p><pre>.*?<\/pre><p>/g, (m) => {
          // Extract the pre part
          return m.slice(4, -3);
        });
        // Fallback: just restore with pre tags inline (will be inside p)
        const restored = line.replace(/\u0000CB(\d+)\u0000/g, (_, idx) => {
          const cb = codeBlocks[Number(idx)];
          return `<pre><code>${escapeHtml(cb.code)}</code></pre>`;
        });
        const formatted = inlineFormat(restored, inlineCodes);
        html += `<p>${formatted}</p>`;
      }
      i++;
      continue;
    }

    // Heading
    const hm = isHeading(line);
    if (hm) {
      const level = hm[1].length;
      const content = inlineFormat(hm[2].trim(), inlineCodes);
      html += `<h${level}>${content}</h${level}>`;
      i++;
      continue;
    }

    if (isHr(line)) {
      html += "<hr />";
      i++;
      continue;
    }

    // Table: header row followed by separator
    if (isTableRow(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const headerCells = line.split("|").map((c) => c.trim()).filter((c) => c.length > 0);
      const sepLine = lines[i + 1];
      // Determine alignment from sep cells
      const aligns = sepLine.split("|").map((c) => c.trim()).filter((c) => c.length > 0).map((c) => {
        if (c.startsWith(":") && c.endsWith(":")) return "center";
        if (c.endsWith(":")) return "right";
        if (c.startsWith(":")) return "left";
        return "";
      });
      let tableHtml = '<div class="data-table rich-table"><table><thead><tr>';
      headerCells.forEach((c, idx) => {
        const al = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
        tableHtml += `<th${al}>${inlineFormat(c, inlineCodes)}</th>`;
      });
      tableHtml += "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && isTableRow(lines[i]) && lines[i].trim() && !isTableSep(lines[i])) {
        const cells = lines[i].split("|").map((c) => c.trim()).filter((c) => c.length > 0);
        // If row has fewer cells than header, pad; if more, truncate
        tableHtml += "<tr>";
        for (let ci = 0; ci < headerCells.length; ci++) {
          const cell = cells[ci] != null ? cells[ci] : "";
          const al = aligns[ci] ? ` style="text-align:${aligns[ci]}"` : "";
          tableHtml += `<td${al}>${inlineFormat(cell, inlineCodes)}</td>`;
        }
        tableHtml += "</tr>";
        i++;
      }
      tableHtml += "</tbody></table></div>";
      html += tableHtml;
      continue;
    }

    // Blockquote: collect consecutive > lines (escaped as &gt;)
    if (isBlockquote(line)) {
      const bqLines = [];
      while (i < lines.length && isBlockquote(lines[i])) {
        bqLines.push(lines[i].replace(/^\s*(?:&gt;\s?|>\s?)/, ""));
        i++;
      }
      const innerRaw = bqLines.join("\n");
      // Recursively render inner as markdown without code-block double-processing? For blockquote we allow inline + paragraphs
      // Simple: join with <br> and inlineFormat
      // If inner contains blank line, split into paragraphs
      const innerParas = innerRaw.split(/\n\s*\n/).map((para) => {
        const paraOneLine = para.replace(/\n/g, "<br />");
        return inlineFormat(paraOneLine, inlineCodes);
      });
      html += `<blockquote>${innerParas.map((p) => `<p>${p}</p>`).join("")}</blockquote>`;
      continue;
    }

    // Unordered list
    if (isUl(line)) {
      html += "<ul>";
      while (i < lines.length && isUl(lines[i])) {
        const item = lines[i].replace(/^\s*[-*]\s+/, "");
        html += `<li>${inlineFormat(item, inlineCodes)}</li>`;
        i++;
      }
      html += "</ul>";
      continue;
    }

    // Ordered list
    if (isOl(line)) {
      html += "<ol>";
      while (i < lines.length && isOl(lines[i])) {
        const item = lines[i].replace(/^\s*\d+\.\s+/, "");
        html += `<li>${inlineFormat(item, inlineCodes)}</li>`;
        i++;
      }
      html += "</ol>";
      continue;
    }

    // Paragraph: collect consecutive lines that are not any other block starter
    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !isHeading(lines[i]) &&
      !isHr(lines[i]) &&
      !isUl(lines[i]) &&
      !isOl(lines[i]) &&
      !isBlockquote(lines[i]) &&
      !lines[i].includes("\u0000CB") &&
      !(isTableRow(lines[i]) && i + 1 < lines.length && isTableSep(lines[i + 1]))
    ) {
      paraLines.push(lines[i]);
      i++;
      // Break if next line is blank (paragraph boundary)
      if (i < lines.length && !lines[i].trim()) break;
    }
    if (paraLines.length) {
      // Join paragraph lines: single newline => <br />, we keep soft breaks as spaces unless double
      // Preserve line breaks inside paragraph as <br />
      const paraText = paraLines.join("\n");
      // If paraText contains a newline, treat as line break
      const withBreaks = paraText.split("\n").map((l) => inlineFormat(l, inlineCodes)).join("<br />");
      html += `<p>${withBreaks}</p>`;
    } else {
      i++;
    }
  }

  // Final sanitize: ensure no leftover placeholders
  html = html.replace(/\u0000CB\d+\u0000/g, "");
  html = html.replace(/\u0000IC\d+\u0000/g, (m) => {
    // Should have been replaced in inlineFormat, but fallback
    const idx = Number(m.match(/\d+/)[0]);
    return `<code>${escapeHtml(inlineCodes[idx] || "")}</code>`;
  });

  return html;
}

/* ---------------- Rendering ---------------- */
async function handleDeleteConversation(convId, convTitle) {
  const title = convTitle || conversations.find((c) => c.id === convId)?.title || "this conversation";
  if (!window.confirm(`Delete "${title}"? This cannot be undone.`)) return;
  const isActiveDelete = activeConversation === convId;
  abortCurrentChat();
  if (isActiveDelete && loadMessagesAbortController) {
    try {
      loadMessagesAbortController.abort();
    } catch (_) {}
  }
  try {
    await api.deleteConversation(convId);
    conversations = conversations.filter((c) => c.id !== convId);
    if (isActiveDelete) {
      if (conversations.length) {
        activeConversation = conversations[0].id;
        $("#project-title").textContent = conversations[0].title;
        await loadMessages(activeConversation);
      } else {
        activeConversation = null;
        $("#project-title").textContent = "No conversation";
        clearStream();
        loadMessagesAbortController = null;
      }
    }
    renderConversations();
  } catch (e) {
    alert(`Could not delete conversation: ${e.message || e}`);
  }
}

async function handleRenameConversation(convId, currentTitle) {
  const existing = conversations.find((c) => c.id === convId)?.title || currentTitle || "";
  const next = window.prompt("Rename conversation", existing);
  if (next === null) return;
  const title = next.trim();
  if (!title || title === existing) return;
  if (title.length > 256) {
    alert("Title is too long (max 256 characters).");
    return;
  }
  // Abort any in-flight chat that might be stale after rename
  abortCurrentChat();
  try {
    const updated = await api.renameConversation(convId, title);
    const idx = conversations.findIndex((c) => c.id === convId);
    if (idx !== -1) conversations[idx] = updated;
    if (activeConversation === convId) {
      $("#project-title").textContent = updated.title;
    }
    renderConversations();
  } catch (e) {
    alert(`Could not rename conversation: ${e.message || e}`);
  }
}

function deriveTitleFromMessage(message) {
  const raw = String(message || "").trim().split("\n")[0].trim();
  if (!raw) return "Untitled";
  return raw.replace(/\s+/g, " ").slice(0, 60).trim() || "Untitled";
}

function isGenericTitle(title) {
  const t = String(title || "").trim();
  return t === "Untitled" || t === "Untitled Document" || t === "Untitled Document (unsaved)" || t === "" || t.startsWith("Untitled");
}

function renderConversations() {
  listEl.innerHTML = "";
  conversations.forEach((conv) => {
    const li = el("li");
    if (conv.id === activeConversation) li.classList.add("active");
    li.dataset.conversationId = conv.id;
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-label", `Open conversation: ${conv.title}`);

    const titleSpan = el("span", "conv-title");
    titleSpan.textContent = conv.title;
    titleSpan.title = conv.title;
    const selectConversation = async () => {
      abortCurrentChat();
      activeConversation = conv.id;
      $("#project-title").textContent = conv.title;
      renderConversations();
      await loadMessages(conv.id);
    };
    // selecting conversation — click on row or title
    li.addEventListener("click", async (e) => {
      if (e.target.closest(".conv-delete") || e.target.closest(".conv-rename")) return;
      await selectConversation();
    });
    li.addEventListener("keydown", async (e) => {
      if (e.target.closest(".conv-delete") || e.target.closest(".conv-rename")) return;
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        await selectConversation();
      }
    });

    titleSpan.addEventListener("dblclick", async (e) => {
      e.stopPropagation();
      await handleRenameConversation(conv.id, conv.title);
    });

    const renameBtn = el("button", "conv-rename");
    renameBtn.type = "button";
    renameBtn.dataset.conversationId = conv.id;
    renameBtn.setAttribute("aria-label", `Rename conversation: ${conv.title}`);
    renameBtn.setAttribute("title", "Rename conversation");
    renameBtn.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
    renameBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await handleRenameConversation(conv.id, conv.title);
    });

    const delBtn = el("button", "conv-delete");
    delBtn.type = "button";
    delBtn.dataset.conversationId = conv.id;
    delBtn.setAttribute("aria-label", `Delete conversation: ${conv.title}`);
    delBtn.setAttribute("title", "Delete conversation");
    delBtn.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>';
    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await handleDeleteConversation(conv.id, conv.title);
    });

    li.appendChild(titleSpan);
    li.appendChild(renameBtn);
    li.appendChild(delBtn);
    listEl.appendChild(li);
  });
}

function renderMessage(msg, meta, isTyping) {
  const wrapper = el("div", `msg ${msg.role}`);
  if (msg.role === "assistant") {
    wrapper.appendChild(el("div", "msg-mark"));
    const body = el("div", "msg-body");
    const content = msg.content || msg.text || "";
    if (isTyping) {
      const p = el("p");
      p.innerHTML = content;
      body.appendChild(p);
    } else {
      const rich = el("div", "rich-text");
      rich.innerHTML = renderMarkdown(content);
      // toolbar: copy button
      const toolbar = el("div", "rich-toolbar");
      const copyBtn = el("button", "rich-copy");
      copyBtn.type = "button";
      copyBtn.textContent = "Copy";
      copyBtn.setAttribute("aria-label", "Copy message");
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(content);
          const prev = copyBtn.textContent;
          copyBtn.textContent = "Copied!";
          setTimeout(() => (copyBtn.textContent = prev), 1200);
        } catch (_) {
          // fallback: select
          const ta = document.createElement("textarea");
          ta.value = content;
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand("copy"); } catch (_) {}
          ta.remove();
          copyBtn.textContent = "Copied!";
          setTimeout(() => (copyBtn.textContent = "Copy"), 1200);
        }
      });
      body.appendChild(rich);
      body.appendChild(toolbar);
      toolbar.appendChild(copyBtn);
    }
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
  const generation = ++loadMessagesGeneration;
  if (loadMessagesAbortController) {
    try {
      loadMessagesAbortController.abort();
    } catch (_) {}
  }
  const controller = new AbortController();
  loadMessagesAbortController = controller;
  clearStream();
  try {
    const data = await api.getConversation(convId, { signal: controller.signal });
    if (generation !== loadMessagesGeneration || convId !== activeConversation) return;
    messagesCache = data.messages || [];
    messagesCache.forEach((m) => renderMessage(m, { model: m.model, node_id: m.node_id, latency_ms: m.latency_ms }));
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (generation !== loadMessagesGeneration || convId !== activeConversation) return;
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
  if (isSending) return;
  const text = (rawText ?? inputEl.value).trim();
  if (!text) return;
  const nodeId = $("#node-select")?.value || null;
  const useStream = $("#stream-toggle")?.checked;
  const requestConvId = activeConversation;
  renderMessage({ role: "user", content: text });
  inputEl.value = "";
  // Optimistically show context-aware title for new or generic conversations
  if (!requestConvId || isGenericTitle(conversations.find((c) => c.id === requestConvId)?.title || "")) {
    $("#project-title").textContent = deriveTitleFromMessage(text);
  }
  const pending = showTyping();
  const myGen = ++chatGeneration;
  const controller = new AbortController();
  chatAbortController = controller;
  setSendingState(true);

  try {
    if (useStream) {
      // Streaming: replace pending with live rich-text accumulation (throttled)
      const bodyP = pending.querySelector(".msg-body p");
      // Convert typing <p> into rich-text container for incremental rendering
      let bodyEl = bodyP;
      if (bodyP) {
        bodyP.className = "rich-text";
        bodyP.innerHTML = "";
        bodyEl = bodyP;
      } else {
        bodyEl = pending.querySelector(".msg-body .rich-text") || pending.querySelector(".msg-body");
      }
      let full = "";
      let latestFull = "";
      let rafId = null;
      let scheduled = false;
      const flushRender = () => {
        if (myGen !== chatGeneration) return;
        bodyEl.innerHTML = renderMarkdown(latestFull);
        stream.scrollTop = stream.scrollHeight;
      };
      const scheduleRender = () => {
        if (scheduled) return;
        scheduled = true;
        const cb = () => {
          scheduled = false;
          rafId = null;
          flushRender();
        };
        if (typeof requestAnimationFrame === "function") {
          rafId = requestAnimationFrame(cb);
        } else {
          rafId = setTimeout(cb, 32);
        }
      };
      const cancelScheduled = () => {
        if (rafId != null) {
          if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId);
          else clearTimeout(rafId);
          rafId = null;
          scheduled = false;
        }
      };
      const meta = await api.sendMessageStream(requestConvId, text, nodeId, (tok) => {
        if (myGen !== chatGeneration) return;
        full += tok;
        latestFull = full;
        scheduleRender();
      }, { signal: controller.signal });
      // Force one final render so the complete response is displayed
      cancelScheduled();
      if (myGen === chatGeneration) {
        latestFull = full;
        flushRender();
      }
      if (myGen !== chatGeneration || controller.signal.aborted) {
        try { pending.remove(); } catch (_) {}
        return;
      }
      pending.remove();
      const replyMeta = meta || {};
      if (myGen !== chatGeneration) return;
      renderMessage({ role: "assistant", content: full || "(empty response)" }, replyMeta);
      if (replyMeta.conversation_id && !activeConversation) {
        activeConversation = replyMeta.conversation_id;
      }
      if (replyMeta.latency_ms) latencyHint.textContent = `${replyMeta.actual_model || ""} · ${replyMeta.latency_ms}ms`;
      // Refresh list if this was a new conversation — also picks up auto-rename for generic titles
      await refreshConversations();
      if (myGen !== chatGeneration) return;
      if (activeConversation) {
        const updated = conversations.find((c) => c.id === activeConversation);
        if (updated) $("#project-title").textContent = updated.title;
      }
    } else {
      const res = await api.sendMessage(requestConvId, text, nodeId, { signal: controller.signal });
      if (myGen !== chatGeneration || controller.signal.aborted) {
        try { pending.remove(); } catch (_) {}
        return;
      }
      pending.remove();
      if (myGen !== chatGeneration) return;
      if (!activeConversation) {
        activeConversation = res.conversation_id;
        $("#project-title").textContent = conversations.find((c) => c.id === activeConversation)?.title || res.conversation_id;
      }
      renderMessage({ role: "assistant", content: res.reply }, res);
      latencyHint.textContent = `${res.actual_model} · ${res.latency_ms}ms`;
      await refreshConversations();
      if (myGen !== chatGeneration) return;
      if (activeConversation) {
        const updated = conversations.find((c) => c.id === activeConversation);
        if (updated) $("#project-title").textContent = updated.title;
      }
    }
  } catch (err) {
    if (myGen !== chatGeneration) return;
    if (isAbortError(err)) {
      try { pending.remove(); } catch (_) {}
      return;
    }
    try { pending.remove(); } catch (_) {}
    const isNetwork = err.message?.includes("Failed to fetch") || err.message?.includes("NetworkError");
    const detail = isNetwork ? "Backend not reachable. Is the FastAPI server running on :8000?" : (err.message || "Unknown error");
    if (myGen !== chatGeneration) return;
    renderMessage({ role: "assistant", content: `Error: ${detail}` });
  } finally {
    if (myGen === chatGeneration && chatAbortController === controller) {
      chatAbortController = null;
      setSendingState(false);
    }
  }
}

function initCompose() {
  setSendingState(false);
  $("#send")?.addEventListener("click", () => {
    if (isSending && chatAbortController) {
      try { chatAbortController.abort(); } catch (_) {}
      return;
    }
    submit();
  });
  inputEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (isSending) return;
      submit();
    }
  });
  $("#new-conversation")?.addEventListener("click", () => {
    abortCurrentChat();
    // Create local draft — server conversation will be auto-created with a context-aware title on first message
    activeConversation = null;
    $("#project-title").textContent = "New conversation";
    clearStream();
    renderConversations();
    inputEl?.focus();
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
