const CATEGORIES = {
  responsibilities: "Обязанности",
  processes: "Процессы",
  people: "Люди",
  systems: "Системы",
  unfinished: "Незавершено",
  decisions: "Решения",
  faqs: "FAQ",
  glossary: "Глоссарий",
  risks: "Риски",
};

const STATUS = {
  draft: "черновик",
  documents: "документы загружены",
  ready: "база собрана",
};

const state = { handoffs: [], current: null, tab: "knowledge", filter: "all" };

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = await res.text();
    try {
      detail = JSON.parse(detail).detail || detail;
    } catch {
      /* keep text */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function show(id) {
  ["view-empty", "view-form", "view-detail"].forEach((v) => $(v).classList.toggle("hidden", v !== id));
}

function renderList() {
  const box = $("handoff-list");
  box.innerHTML = state.handoffs
    .map(
      (h) => `<li><button type="button" data-id="${h.id}" class="${state.current?.id === h.id ? "active" : ""}">
        <b>${escapeHtml(h.employee_name)}</b>
        <span>${escapeHtml(h.role)} · ${h.document_count} док. · ${h.knowledge_count} карточек</span>
      </button></li>`
    )
    .join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => openHandoff(Number(btn.dataset.id));
  });
}

async function refreshList() {
  state.handoffs = await api("/api/handoffs");
  renderList();
}

async function openHandoff(id) {
  state.current = await api(`/api/handoffs/${id}`);
  state.filter = "all";
  renderList();
  renderDetail();
  show("view-detail");
}

function renderDetail() {
  const h = state.current;
  $("d-name").textContent = h.employee_name;
  $("d-role").textContent = h.role;
  $("d-dept").textContent = h.department || "Без подразделения";
  $("d-status").textContent = STATUS[h.status] || h.status;
  $("d-summary").textContent = h.role_summary || "Сводка появится после сборки базы знаний.";
  $("d-successor").textContent = `Преемник: ${h.successor_name || "не указан"}`;
  $("d-lastday").textContent = `Последний день: ${h.last_day || "не указан"}`;
  $("d-notes").textContent = h.notes ? `Заметка: ${h.notes}` : "";
  $("btn-playbook").href = `/api/handoffs/${h.id}/playbook.md`;

  $("doc-list").innerHTML = h.documents.length
    ? h.documents
        .map((d) => `<li><span>${escapeHtml(d.filename)}</span><span>${d.char_count} симв.</span></li>`)
        .join("")
    : "<li>Пока пусто — загрузите файлы сотрудника.</li>";

  renderKnowledge();
  renderChat();
}

function renderKnowledge() {
  const items = state.current.knowledge || [];
  const cats = [...new Set(items.map((i) => i.category))];
  $("cat-filters").innerHTML =
    `<button type="button" data-cat="all" class="${state.filter === "all" ? "active" : ""}">Все (${items.length})</button>` +
    cats
      .map(
        (c) =>
          `<button type="button" data-cat="${c}" class="${state.filter === c ? "active" : ""}">${CATEGORIES[c] || c}</button>`
      )
      .join("");
  $("cat-filters").querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      state.filter = btn.dataset.cat;
      renderKnowledge();
    };
  });
  const shown = items.filter((i) => state.filter === "all" || i.category === state.filter);
  $("knowledge-cards").innerHTML = shown.length
    ? shown
        .map(
          (i) => `<article class="card">
            <h4>${escapeHtml(i.title)}</h4>
            <p>${escapeHtml(i.body)}</p>
            <div class="src">${CATEGORIES[i.category] || i.category}${i.source ? " · " + escapeHtml(i.source) : ""} · ${i.confidence}</div>
          </article>`
        )
        .join("")
    : "<p class='hint'>Карточек нет. Загрузите документы и нажмите «Собрать базу знаний».</p>";
}

function renderChat() {
  const log = $("chat-log");
  const messages = state.current.messages || [];
  log.innerHTML = messages
    .map((m) => {
      const cites = (m.citations || [])
        .slice(0, 3)
        .map((c) => `<div class="cite">${escapeHtml(c.filename)}</div>`)
        .join("");
      return `<div class="bubble ${m.role}">${escapeHtml(m.content)}${m.role === "assistant" ? cites : ""}</div>`;
    })
    .join("");
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

$("btn-new").onclick = () => show("view-form");
$("btn-cancel-form").onclick = () => show(state.current ? "view-detail" : "view-empty");

$("create-form").onsubmit = async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  const created = await api("/api/handoffs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  e.target.reset();
  await refreshList();
  await openHandoff(created.id);
};

$("btn-demo").onclick = async () => {
  $("process-hint").textContent = "";
  const created = await api("/api/demo", { method: "POST" });
  await refreshList();
  await openHandoff(created.id);
};

$("upload-form").onsubmit = async (e) => {
  e.preventDefault();
  const file = e.target.file.files[0];
  if (!file || !state.current) return;
  const body = new FormData();
  body.append("file", file);
  $("process-hint").textContent = "Загрузка…";
  try {
    await api(`/api/handoffs/${state.current.id}/documents`, { method: "POST", body });
    e.target.reset();
    await refreshList();
    await openHandoff(state.current.id);
    $("process-hint").textContent = "Файл принят. Можно собирать базу.";
  } catch (err) {
    $("process-hint").textContent = err.message;
  }
};

$("btn-process").onclick = async () => {
  if (!state.current) return;
  $("process-hint").textContent = "Индексация и извлечение знаний…";
  try {
    await api(`/api/handoffs/${state.current.id}/process`, { method: "POST" });
    await refreshList();
    await openHandoff(state.current.id);
    $("process-hint").textContent = "База знаний готова.";
  } catch (err) {
    $("process-hint").textContent = err.message;
  }
};

$("btn-delete").onclick = async () => {
  if (!state.current) return;
  if (!confirm("Удалить эту эстафету и все документы?")) return;
  await api(`/api/handoffs/${state.current.id}`, { method: "DELETE" });
  state.current = null;
  await refreshList();
  show("view-empty");
};

$("tabs").onclick = (e) => {
  if (e.target.tagName !== "BUTTON") return;
  state.tab = e.target.dataset.tab;
  $("tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === e.target));
  $("tab-knowledge").classList.toggle("hidden", state.tab !== "knowledge");
  $("tab-chat").classList.toggle("hidden", state.tab !== "chat");
};

$("ask-form").onsubmit = async (e) => {
  e.preventDefault();
  const q = new FormData(e.target).get("question");
  if (!q || !state.current) return;
  e.target.reset();
  try {
    await api(`/api/handoffs/${state.current.id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    await openHandoff(state.current.id);
    state.tab = "chat";
    $("tabs").querySelector('[data-tab="chat"]').click();
  } catch (err) {
    alert(err.message);
  }
};

(async function init() {
  try {
    const health = await api("/api/health");
    $("llm-flag").textContent = health.llm
      ? "LLM подключён: карточки и ответы будут связными."
      : "Работает без LLM: поиск по фрагментам и эвристики. Добавьте LLM_API_KEY в .env для полной версии.";
    await refreshList();
  } catch (err) {
    $("llm-flag").textContent = `Сервер недоступен: ${err.message}`;
  }
})();
