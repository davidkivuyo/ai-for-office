/**
 * Nexus.ai — frontend only.
 * Every backend touchpoint is isolated in the `api` object below so a Python
 * backend (Flask/FastAPI/Django) can be wired in later without touching the UI.
 */

const api = {
  // POST /api/chat  -> { reply, table?, files? }
  async sendMessage(_conversationId, _text, _attachments) {
    throw new Error("Backend not connected yet");
  },
  // GET /api/conversations
  async listConversations() {
    throw new Error("Backend not connected yet");
  },
  // GET /api/database/tables
  async listDatabaseTables() {
    throw new Error("Backend not connected yet");
  },
};

/* ---------------- Demo state (replaced by API data later) ---------------- */

const conversations = [
  { id: "c1", title: "Q3 Revenue Analysis" },
  { id: "c2", title: "Employee Feedback Loop" },
  { id: "c3", title: "Supply Chain Audit" },
];

const seedMessages = [
  {
    role: "assistant",
    text: "I've successfully accessed the <strong>Corporate_Finance_2023</strong> database. Based on the query, I see a 14% deviation in regional expenditure for the North sector. Should I generate a summary table for the executive report or draft the Excel reconciliation file?",
    suggestions: ["Generate Table", "Reconcile Excel"],
  },
  {
    role: "user",
    text: "Summarize the North sector deviations and format them as a table compatible with Word. Also, save this query to my profile for future reference.",
  },
  {
    role: "assistant",
    text: "Drafting the summary table now...",
    table: {
      head: ["Category", "Projected", "Actual", "Variance"],
      rows: [
        ["Operations", "$450,000", "$512,000", { value: "+13.7%", tone: "neg" }],
        ["Logistics", "$210,000", "$198,000", { value: "-5.7%", tone: "pos" }],
      ],
    },
    files: [
      { badge: "XLSX", name: "North_Sector_Variance.xlsx", meta: "Generated just now · 24.5 KB" },
    ],
  },
];

let activeConversation = conversations[0].id;
let attachments = [];

/* ---------------- DOM helpers ---------------- */

const $ = (sel) => document.querySelector(sel);
const stream = $("#stream");
const listEl = $("#conversation-list");
const inputEl = $("#composer-input");
const attachEl = $("#attachments");

function el(tag, className, html) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

/* ---------------- Rendering ---------------- */

function renderConversations() {
  listEl.innerHTML = "";
  conversations.forEach((conv) => {
    const li = el("li", conv.id === activeConversation ? "active" : "", conv.title);
    li.addEventListener("click", () => {
      activeConversation = conv.id;
      $("#project-title").textContent = conv.title;
      renderConversations();
    });
    listEl.appendChild(li);
  });
}

function renderTable(table) {
  const wrap = el("div", "data-table");
  const t = el("table");
  const thead = el("thead");
  const hr = el("tr");
  table.head.forEach((h) => hr.appendChild(el("th", "", h)));
  thead.appendChild(hr);
  const tbody = el("tbody");
  table.rows.forEach((row) => {
    const tr = el("tr");
    row.forEach((cell) => {
      const isObj = typeof cell === "object";
      tr.appendChild(el("td", isObj ? cell.tone : "", isObj ? cell.value : cell));
    });
    tbody.appendChild(tr);
  });
  t.append(thead, tbody);
  wrap.appendChild(t);
  return wrap;
}

function renderFile(file) {
  const card = el("div", "file-card");
  card.append(
    el("div", "file-badge", file.badge),
    el("div", "", `<p class="file-name">${file.name}</p><p class="file-meta">${file.meta}</p>`),
    el("button", "file-action", "Download"),
  );
  return card;
}

function renderMessage(msg) {
  const wrapper = el("div", `msg ${msg.role}`);

  if (msg.role === "assistant") {
    wrapper.appendChild(el("div", "msg-mark"));
    const body = el("div", "msg-body");
    body.appendChild(el("p", "", msg.text));

    if (msg.suggestions) {
      const row = el("div", "suggestions");
      msg.suggestions.forEach((s) => {
        const chip = el("button", "chip", s);
        chip.addEventListener("click", () => submit(s));
        row.appendChild(chip);
      });
      body.appendChild(row);
    }
    if (msg.table) body.appendChild(renderTable(msg.table));
    if (msg.files) msg.files.forEach((f) => body.appendChild(renderFile(f)));
    wrapper.appendChild(body);
  } else {
    const bubble = el("div", "bubble");
    bubble.appendChild(el("p", "", msg.text));
    wrapper.appendChild(bubble);
  }

  stream.appendChild(wrapper);
  stream.scrollTop = stream.scrollHeight;
  return wrapper;
}

function renderAttachments() {
  attachEl.innerHTML = "";
  attachments.forEach((name, i) => {
    const pill = el("div", "attachment", `<span>${name}</span>`);
    const x = el("button", "", "✕");
    x.addEventListener("click", () => {
      attachments.splice(i, 1);
      renderAttachments();
    });
    pill.appendChild(x);
    attachEl.appendChild(pill);
  });
}

/* ---------------- Interactions ---------------- */

function escapeHtml(str) {
  return str.replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

async function submit(rawText) {
  const text = (rawText ?? inputEl.value).trim();
  if (!text) return;

  renderMessage({ role: "user", text: escapeHtml(text) });
  inputEl.value = "";
  const sent = attachments.slice();
  attachments = [];
  renderAttachments();

  const pending = renderMessage({
    role: "assistant",
    text: '<span class="typing"><span></span><span></span><span></span></span>',
  });

  try {
    const res = await api.sendMessage(activeConversation, text, sent);
    pending.remove();
    renderMessage({ role: "assistant", ...res });
  } catch (_err) {
    pending.querySelector("p").innerHTML =
      "The assistant service isn't connected yet. Once the Python backend is wired to <strong>/api/chat</strong>, replies, generated Word and Excel files, and saved conversations will stream in here.";
  }
}

function init() {
  renderConversations();
  seedMessages.forEach(renderMessage);
  renderAttachments();

  $("#send").addEventListener("click", () => submit());
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });

  document.querySelectorAll("[data-attach]").forEach((btn) => {
    btn.addEventListener("click", () => {
      attachments.push(btn.dataset.attach);
      renderAttachments();
    });
  });

  $("#new-conversation").addEventListener("click", () => {
    const conv = { id: `c${Date.now()}`, title: "Untitled Document" };
    conversations.unshift(conv);
    activeConversation = conv.id;
    $("#project-title").textContent = conv.title;
    stream.innerHTML = "";
    renderConversations();
  });
}

init();
