"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const STATUS_TEXT = { idle: "Bo'sh", waiting: "Navbatda", working: "Ishlayapti", done: "Tayyor", skipped: "Kerak emas", error: "Xato" };
const SCREEN_TEXT = { idle: "—", waiting: "topshiriq oldi", done: "✓ tayyor", skipped: "bu loyihada kerak emas", error: "✕ xato" };
const RUN_TEXT = { idle: "Kutmoqda", running: "Agentlar ishlayapti", done: "Tayyor", error: "Xato", stopped: "To'xtatilgan" };
const SEVERITY_TEXT = { critical: "juda jiddiy", high: "jiddiy", medium: "o'rtacha", low: "kichik" };
const EXAMPLES = [
  "Kofexona uchun sayt: menyu, narxlar va stol band qilish",
  "Shaxsiy portfolio sayt, loyihalar galereyasi bilan",
  "Vazifalar ro'yxati (to-do) ilovasi, muddat va filtrlar bilan",
  "Fitnes klub uchun landing sahifa, tariflar va ariza formasi",
];

const state = { agents: [], byId: {}, projects: [], current: null, source: null, lastSeq: 0, replaying: true, desks: {}, files: [], checks: 0 };

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "So'rov bajarilmadi (" + res.status + ")");
  return data;
}

function initials(name) {
  return name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

function avatar(agent, small) {
  const node = el("span", "avatar" + (small ? " sm" : ""), initials(agent.name));
  node.style.setProperty("--agent", agent.color);
  node.setAttribute("aria-hidden", "true");
  return node;
}

// --- office floor -----------------------------------------------------------
function buildDesks() {
  const wrap = $("desks");
  wrap.replaceChildren();
  for (const agent of state.agents) {
    const desk = el("article", "desk idle");
    desk.style.setProperty("--agent", agent.color);
    const screen = el("div", "screen");
    const stream = el("pre");
    const stateText = el("div", "state", SCREEN_TEXT.idle);
    screen.append(stream, stateText);
    const plate = el("div", "nameplate");
    const who = el("div", "who");
    who.append(el("strong", "", agent.name), el("span", "", agent.role));
    plate.append(avatar(agent), who);
    const task = el("p", "task", agent.mission);
    const foot = el("div", "desk-foot");
    const badge = el("span", "badge");
    const badgeText = el("span", "", STATUS_TEXT.idle);
    badge.append(el("i"), badgeText);
    const model = el("span", "model");
    foot.append(badge, model);
    desk.append(screen, plate, task, foot);
    wrap.append(desk);
    state.desks[agent.id] = { desk, stream, stateText, task, badgeText, model, text: "", status: "idle", agent };
  }
}

function setAgent(id, status, task) {
  const d = state.desks[id];
  if (!d) return;
  d.status = status;
  d.desk.className = "desk " + status;
  d.badgeText.textContent = STATUS_TEXT[status] || status;
  d.task.textContent = task || d.agent.mission;
  if (status === "working") {
    d.text = "";
    d.stream.textContent = "";
    d.stateText.hidden = false;
    d.stateText.textContent = "o'ylayapti…";
  } else {
    d.stream.textContent = "";
    d.stateText.hidden = false;
    d.stateText.textContent = SCREEN_TEXT[status] || "";
  }
  updateOfficeSummary();
}

function streamText(id, text) {
  const d = state.desks[id];
  if (!d || d.status !== "working") return;
  d.text = (d.text + text).slice(-700);
  d.stateText.hidden = true;
  d.stream.textContent = d.text.split("\n").slice(-6).join("\n");
}

function resetDesks() {
  for (const agent of state.agents) setAgent(agent.id, "idle", "");
  for (const d of Object.values(state.desks)) d.model.textContent = "";
}

function updateOfficeSummary() {
  const all = Object.values(state.desks);
  const working = all.filter((d) => d.status === "working").length;
  const done = all.filter((d) => d.status === "done").length;
  $("office-summary").textContent = working || done ? `${working} ta ishlayapti · ${done} ta tugatdi` : `${all.length} agent tayyor`;
}

// --- chat ---------------------------------------------------------------------
function chatAppend(node) {
  const chat = $("chat");
  const empty = chat.querySelector(".empty");
  if (empty) empty.remove();
  const pinned = chat.parentElement.scrollHeight - chat.parentElement.scrollTop - chat.parentElement.clientHeight < 80;
  chat.append(node);
  if (!state.replaying && pinned) chat.parentElement.scrollTop = chat.parentElement.scrollHeight;
}

function chatMessage(event) {
  const agent = state.byId[event.agent];
  if (!agent) return;
  const row = el("div", "msg");
  const body = el("div", "msg-body");
  const head = el("div", "msg-head", agent.name);
  if (event.to && state.byId[event.to]) head.append(el("span", "to", " → " + state.byId[event.to].name));
  body.append(head, el("div", "msg-text", event.text));
  row.append(avatar(agent, true), body);
  chatAppend(row);
}

function chatSystem(text, code) {
  const row = el("div", "sys", text);
  if (code) row.append(el("code", "", code));
  chatAppend(row);
}

// --- checks -------------------------------------------------------------------
function addCheckGroup(title, items, emptyText) {
  $("checks-empty").hidden = true;
  const group = el("section", "check-group");
  group.append(el("h3", "", title));
  if (!items.length) group.append(el("p", "clean", emptyText));
  for (const item of items) {
    const card = el("div", "issue " + (item.severity || "medium"));
    const meta = el("div", "meta");
    meta.append(el("span", "", SEVERITY_TEXT[item.severity] || item.severity || ""), el("code", "", item.file || ""));
    card.append(meta, el("div", "", item.title || item.message || ""));
    if (item.details) card.append(el("div", "fix", item.details));
    if (item.fix) card.append(el("div", "fix", "Tuzatish: " + item.fix));
    group.append(card);
  }
  $("checks").prepend(group);
  state.checks += items.length;
  $("checks-count").textContent = state.checks || "";
}

// --- files ----------------------------------------------------------------------
let filesTimer = null;
function refreshFilesSoon() {
  clearTimeout(filesTimer);
  filesTimer = setTimeout(refreshFiles, state.replaying ? 0 : 600);
}

async function refreshFiles() {
  if (!state.current) return;
  const pid = state.current.id;
  try {
    const data = await api(`api/projects/${pid}`);
    if (!state.current || state.current.id !== pid) return;
    state.files = data.files;
    const list = $("files");
    list.replaceChildren();
    for (const file of data.files) {
      const item = el("li");
      const button = el("button");
      button.type = "button";
      button.append(el("span", "", file.path), el("span", "size", formatSize(file.size)));
      button.addEventListener("click", () => openFile(file.path));
      item.append(button);
      list.append(item);
    }
    $("files-empty").hidden = data.files.length > 0;
    $("files-count").textContent = data.files.length || "";
  } catch (_) { /* next event retries */ }
}

function formatSize(bytes) {
  return bytes < 1024 ? bytes + " B" : (bytes / 1024).toFixed(1) + " KB";
}

async function openFile(path) {
  try {
    const data = await api(`api/projects/${state.current.id}/file?path=${encodeURIComponent(path)}`);
    $("file-title").textContent = path;
    $("file-content").textContent = data.content;
    $("file-viewer").showModal();
  } catch (e) { alert(e.message); }
}

// --- preview --------------------------------------------------------------------
function showPreview(version) {
  const base = `sites/${state.current.id}/`;
  $("preview-card").hidden = false;
  $("preview-version").textContent = version ? `v${version}` : "";
  $("preview-frame").src = `${base}?v=${version || Date.now()}`;
  $("preview-open").href = base;
  $("preview-zip").href = `api/projects/${state.current.id}/zip`;
}

// --- events -----------------------------------------------------------------------
function setRun(status, message) {
  const running = status === "running";
  state.current.status = status;
  const pill = $("run-status");
  pill.className = "pill " + status;
  pill.textContent = RUN_TEXT[status] || status;
  $("stop-run").hidden = !running;
  $("rebuild").hidden = running;
  const built = state.files.some((f) => f.path === "index.html") || !$("preview-card").hidden;
  $("change-form").hidden = running || !built;
  const note = $("run-message");
  note.hidden = !message;
  note.textContent = message || "";
  note.className = "run-message" + (status === "error" ? " error" : status === "done" ? " ok" : "");
}

function handle(event) {
  if (event.seq) state.lastSeq = event.seq;
  switch (event.type) {
    case "run_start":
      resetDesks();
      chatAppend(el("div", "divider", (event.kind === "build" ? "Yangi qurilish: " : "O'zgartirish: ") + event.request));
      setRun("running", "");
      break;
    case "agent": setAgent(event.agent, event.status, event.task); break;
    case "delta": streamText(event.agent, event.text); break;
    case "delta_reset": if (state.desks[event.agent]) { state.desks[event.agent].text = ""; state.desks[event.agent].stream.textContent = ""; } break;
    case "notice": if (state.desks[event.agent]) state.desks[event.agent].model.textContent = event.text; break;
    case "llm": if (state.desks[event.agent]) state.desks[event.agent].model.textContent = event.model; break;
    case "message": chatMessage(event); break;
    case "file": {
      const agent = state.byId[event.agent];
      const verb = { created: "yaratdi", updated: "qayta yozdi", edited: "tahrirladi" }[event.action] || "yozdi";
      chatSystem(`${agent ? agent.name : ""} ${verb}: `, event.path);
      refreshFilesSoon();
      break;
    }
    case "check":
      addCheckGroup(event.label + (event.browser ? " (statik + brauzer)" : " (statik)"), event.issues, "Muammo topilmadi");
      chatSystem(`Xato qidiruvchi · ${event.label}: ${event.issues.length} ta muammo`);
      break;
    case "bugs":
      addCheckGroup((state.byId[event.agent] || {}).name + " topilmalari", event.bugs, "Topilma yo'q");
      break;
    case "project":
      state.current.name = event.name;
      $("project-name").textContent = event.name;
      renderProjectSelect();
      break;
    case "preview": showPreview(event.version); refreshFilesSoon(); break;
    case "run_end": setRun(event.status, event.message); refreshFilesSoon(); break;
    case "ready":
      state.replaying = false;
      $("chat").parentElement.scrollTop = $("chat").parentElement.scrollHeight;
      if (!event.running && state.current.status === "running") setRun("stopped", "");
      break;
  }
}

function connect() {
  if (state.source) state.source.close();
  if (!state.current) return;
  const pid = state.current.id;
  const source = new EventSource(`api/projects/${pid}/events?after=${state.lastSeq}`);
  state.source = source;
  source.onmessage = (message) => {
    if (state.current && state.current.id === pid) handle(JSON.parse(message.data));
  };
  source.onerror = () => {
    source.close();
    if (state.source === source) setTimeout(() => { if (state.source === source) connect(); }, 2000);
  };
}

// --- projects ---------------------------------------------------------------------
function renderProjectSelect() {
  const select = $("project-select");
  select.replaceChildren();
  for (const project of state.projects) {
    const option = el("option", "", project.name);
    option.value = project.id;
    select.append(option);
  }
  select.hidden = state.projects.length === 0;
  $("new-project").hidden = state.projects.length === 0;
  if (state.current) select.value = state.current.id;
}

function showComposer() {
  if (state.source) state.source.close();
  state.source = null;
  state.current = null;
  history.replaceState(null, "", location.pathname);
  $("composer").hidden = false;
  $("project-bar").hidden = true;
  $("preview-card").hidden = true;
  clearPanels();
  resetDesks();
  renderProjectSelect();
  $("project-select").selectedIndex = -1;
  $("brief").focus();
}

function clearPanels() {
  $("chat").replaceChildren(el("p", "empty", "Agentlar ishni boshlaganda ularning yozishmalari shu yerda ko'rinadi."));
  $("checks").replaceChildren();
  $("checks-empty").hidden = false;
  $("files").replaceChildren();
  $("files-empty").hidden = false;
  $("files-count").textContent = "";
  $("checks-count").textContent = "";
  state.checks = 0;
  state.files = [];
}

function openProject(project) {
  state.current = project;
  state.lastSeq = 0;
  state.replaying = true;
  history.replaceState(null, "", "#p=" + project.id);
  $("composer").hidden = true;
  $("project-bar").hidden = false;
  $("preview-card").hidden = true;
  $("project-name").textContent = project.name;
  $("project-brief").textContent = project.brief;
  clearPanels();
  resetDesks();
  setRun(project.status, "");
  renderProjectSelect();
  refreshFiles();
  connect();
}

// --- settings ---------------------------------------------------------------------
function renderSettings(settings) {
  state.settings = settings;
  $("key-dot").className = "dot" + (settings.has_key ? " ok" : "");
  $("set-provider").value = settings.provider;
  $("set-models").value = settings.models.join(", ");
  $("set-base").value = settings.base_url;
  $("set-key-hint").textContent = settings.has_key ? `(saqlangan: ${settings.key_hint})` : "(kiritilmagan)";
  $("set-base-wrap").hidden = settings.provider !== "openai";
}

// --- wiring -------------------------------------------------------------------------
async function init() {
  const data = await api("api/state");
  state.agents = data.agents;
  state.byId = Object.fromEntries(data.agents.map((a) => [a.id, a]));
  state.projects = data.projects;
  buildDesks();
  updateOfficeSummary();
  renderSettings(data.settings);
  for (const text of EXAMPLES) {
    const chip = el("button", "chip", text);
    chip.type = "button";
    chip.addEventListener("click", () => { $("brief").value = text; $("brief").focus(); });
    $("examples").append(chip);
  }
  const wanted = (location.hash.match(/p=([0-9a-f]+)/) || [])[1];
  const project = state.projects.find((p) => p.id === wanted) || state.projects[0];
  if (project) openProject(project); else renderProjectSelect();

  $("brief-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("brief").value.trim();
    if (text.length < 3) return $("brief").focus();
    const button = $("brief-submit");
    button.disabled = true;
    try {
      const created = await api("api/projects", { method: "POST", body: { text } });
      state.projects.unshift(created);
      $("brief").value = "";
      openProject(created);
    } catch (e) {
      alert(e.message);
      if (!state.settings.has_key) $("settings").showModal();
    } finally { button.disabled = false; }
  });

  $("change-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("change").value.trim();
    if (text.length < 3) return;
    try {
      await api(`api/projects/${state.current.id}/change`, { method: "POST", body: { text } });
      $("change").value = "";
    } catch (e) { alert(e.message); }
  });

  $("stop-run").addEventListener("click", () => api(`api/projects/${state.current.id}/stop`, { method: "POST" }).catch((e) => alert(e.message)));
  $("rebuild").addEventListener("click", () => {
    if (confirm("Loyiha boshidan qayta qurilsinmi? Mavjud fayllar qayta yoziladi."))
      api(`api/projects/${state.current.id}/rebuild`, { method: "POST" }).catch((e) => alert(e.message));
  });
  $("delete-project").addEventListener("click", async () => {
    if (!confirm("Loyiha va uning barcha fayllari o'chirilsinmi?")) return;
    const pid = state.current.id;
    try {
      await api(`api/projects/${pid}`, { method: "DELETE" });
      state.projects = state.projects.filter((p) => p.id !== pid);
      state.projects.length ? openProject(state.projects[0]) : showComposer();
    } catch (e) { alert(e.message); }
  });
  $("project-select").addEventListener("change", (event) => {
    const picked = state.projects.find((p) => p.id === event.target.value);
    if (picked) openProject(picked);
  });
  $("new-project").addEventListener("click", showComposer);

  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === tab));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + tab.dataset.tab));
    });
  }
  for (const button of document.querySelectorAll(".segmented button")) {
    button.addEventListener("click", () => {
      document.querySelectorAll(".segmented button").forEach((b) => b.classList.toggle("active", b === button));
      $("preview-frame-wrap").classList.toggle("mobile", button.dataset.device === "mobile");
    });
  }
  $("preview-reload").addEventListener("click", () => { $("preview-frame").src = `sites/${state.current.id}/?v=${Date.now()}`; });
  $("file-close").addEventListener("click", () => $("file-viewer").close());

  $("open-settings").addEventListener("click", () => { $("set-message").hidden = true; $("settings").showModal(); });
  $("set-cancel").addEventListener("click", () => $("settings").close());
  $("set-provider").addEventListener("change", (event) => { $("set-base-wrap").hidden = event.target.value !== "openai"; });
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const note = $("set-message");
    try {
      const result = await api("api/settings", { method: "POST", body: {
        provider: $("set-provider").value, api_key: $("set-key").value.trim(),
        models: $("set-models").value, base_url: $("set-base").value,
      } });
      $("set-key").value = "";
      renderSettings(result.settings);
      note.textContent = result.message;
      note.className = "run-message " + (result.ok ? "ok" : "error");
    } catch (e) {
      note.textContent = e.message;
      note.className = "run-message error";
    }
    note.hidden = false;
  });
}

init().catch((e) => { document.body.prepend(el("p", "run-message error", "Ofis yuklanmadi: " + e.message)); });
