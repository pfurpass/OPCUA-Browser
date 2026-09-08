"use strict";

const $ = (id) => document.getElementById(id);
const ROOT_ID = "i=84";
const OBJECTS_ID = "i=85";

const S = {
  session: null,
  readOnly: false,
  selectedNode: null,
  selectedClass: null,
  pollTimer: null,
  samples: [],
  refCache: new Map(),
  tab: "value",
};

/* ------------------------------------------------------------------ Werkzeug */
function sp(suffix) {
  return `/api/sessions/${encodeURIComponent(S.session)}${suffix}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* leerer Rumpf */ }
  if (!res.ok) {
    const error = new Error(data.detail || `Der Dienst antwortete mit ${res.status}.`);
    error.status = res.status;
    if (res.status === 409 || res.status === 404) resetUi(error.message);
    throw error;
  }
  return data;
}

function status(text, state = "") {
  const node = $("status-msg");
  node.textContent = text || "";
  node.dataset.state = state;
}

function link(state, text) {
  $("link-state").querySelector(".led").dataset.state = state;
  $("link-text").textContent = text;
}

/* --------------------------------------------------------------- Verbindung */
$("connect-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (S.session) return;
  const url = $("url").value.trim();
  if (!url) { status("Bitte eine Endpunktadresse eingeben.", "bad"); return; }

  $("connect-btn").disabled = true;
  link("busy", "verbinde …");
  status("");
  try {
    const info = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({
        url,
        username: $("username").value || null,
        password: $("password").value || null,
        policy: $("policy").value,
        mode: $("mode").value,
      }),
    });
    S.session = info.sessionId;
    S.readOnly = info.readOnly;
    link("good", "verbunden");
    $("connect-btn").hidden = true;
    $("disconnect-btn").hidden = false;
    $("url").disabled = true;
    $("search").disabled = false;
    $("search-btn").disabled = false;
    $("status-server").textContent = info.serverName || "Server ohne Namen";
    $("status-url").textContent = info.url;
    $("status-ns").textContent = `${info.namespaces.length} Namensräume · Sicherheit: ${info.security}`;
    localStorage.setItem("opcua.url", url);
    await startTree();
  } catch (err) {
    link("bad", "nicht verbunden");
    status(err.message, "bad");
  } finally {
    $("connect-btn").disabled = false;
  }
});

$("disconnect-btn").addEventListener("click", async () => {
  const id = S.session;
  S.session = null;
  if (id) await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
  resetUi("Verbindung getrennt.", "");
});

function resetUi(message, state = "bad") {
  stopPolling();
  S.session = null;
  S.selectedNode = null;
  S.refCache.clear();
  $("tree").innerHTML = "";
  $("tree-placeholder").hidden = false;
  $("tree-foot").textContent = "";
  $("node-view").hidden = true;
  $("detail-placeholder").hidden = false;
  $("search-results").hidden = true;
  $("search").disabled = true;
  $("search-btn").disabled = true;
  $("connect-btn").hidden = false;
  $("disconnect-btn").hidden = true;
  $("url").disabled = false;
  $("status-server").textContent = "–";
  $("status-url").textContent = "";
  $("status-ns").textContent = "";
  link("off", "nicht verbunden");
  status(message || "", state);
}

$("options-btn").addEventListener("click", () => {
  const box = $("options");
  box.hidden = !box.hidden;
  $("options-btn").setAttribute("aria-expanded", String(!box.hidden));
});

$("endpoints-btn").addEventListener("click", async () => {
  const url = $("url").value.trim();
  const list = $("endpoint-list");
  if (!url) { status("Bitte zuerst eine Adresse eingeben.", "bad"); return; }
  list.hidden = false;
  list.textContent = "frage Endpunkte ab …";
  try {
    const data = await api("/api/endpoints", { method: "POST", body: JSON.stringify({ url }) });
    list.innerHTML = "";
    if (!data.endpoints.length) { list.textContent = "Der Server nennt keine Endpunkte."; return; }
    for (const ep of data.endpoints) {
      const row = document.createElement("div");
      row.innerHTML = `<b>${esc(ep.policy)}</b> / ${esc(ep.mode)} · ${esc(ep.endpointUrl)} · Anmeldung: ${esc(ep.tokens.join(", ") || "–")}`;
      list.appendChild(row);
    }
  } catch (err) {
    list.textContent = err.message;
  }
});

/* --------------------------------------------------------------------- Baum */
function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function makeItem(entry, parentKey) {
  const li = document.createElement("li");
  li.setAttribute("role", "none");
  li.dataset.key = parentKey ? `${parentKey}|${entry.nodeId}` : entry.nodeId;
  li.dataset.nodeId = entry.nodeId;
  li.dataset.nodeClass = entry.nodeClass;
  li.dataset.hasChildren = entry.hasChildren === false ? "0" : "1";

  const row = document.createElement("div");
  row.className = "row";
  row.setAttribute("role", "treeitem");
  row.setAttribute("aria-selected", "false");

  const caret = document.createElement("button");
  caret.type = "button";
  caret.className = "caret" + (li.dataset.hasChildren === "0" ? " leaf" : "");
  caret.setAttribute("aria-expanded", "false");
  caret.setAttribute("aria-label", "Auf- oder zuklappen");
  caret.addEventListener("click", (e) => { e.stopPropagation(); toggle(li); });

  const glyph = document.createElement("span");
  glyph.className = "glyph";
  glyph.dataset.class = entry.nodeClass;

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = entry.displayName;
  name.title = `${entry.displayName} · ${entry.browseName} · ${entry.nodeId}`;

  row.append(caret, glyph, name);

  if (entry.preview !== undefined) {
    const preview = document.createElement("span");
    preview.className = "preview";
    preview.dataset.status = entry.status || "good";
    preview.textContent = entry.preview;
    row.appendChild(preview);
  }

  row.addEventListener("click", () => select(li));
  row.addEventListener("dblclick", () => toggle(li));
  li.appendChild(row);
  return li;
}

async function loadChildren(li) {
  if (li.dataset.loaded === "1") return true;
  li.querySelector(".row").classList.add("loading");
  try {
    const data = await api(sp(`/browse?nodeId=${encodeURIComponent(li.dataset.nodeId)}`));
    if (!data.children.length) {
      li.dataset.hasChildren = "0";
      li.querySelector(".caret").classList.add("leaf");
      li.dataset.loaded = "1";
      return false;
    }
    const ul = document.createElement("ul");
    ul.setAttribute("role", "group");
    for (const child of data.children) ul.appendChild(makeItem(child, li.dataset.key));
    li.appendChild(ul);
    li.dataset.loaded = "1";
    return true;
  } catch (err) {
    status(err.message, "bad");
    return false;
  } finally {
    li.querySelector(".row").classList.remove("loading");
  }
}

async function expand(li) {
  if (li.dataset.hasChildren === "0") return;
  const ok = await loadChildren(li);
  if (!ok) return;
  li.dataset.expanded = "1";
  li.querySelector(".caret").setAttribute("aria-expanded", "true");
  const ul = li.querySelector(":scope > ul");
  if (ul) ul.hidden = false;
  countNodes();
}

function collapse(li) {
  li.dataset.expanded = "0";
  li.querySelector(".caret").setAttribute("aria-expanded", "false");
  const ul = li.querySelector(":scope > ul");
  if (ul) ul.hidden = true;
  countNodes();
}

function toggle(li) {
  if (li.dataset.expanded === "1") collapse(li); else expand(li);
}

function countNodes() {
  const rows = visibleRows().length;
  $("tree-foot").textContent = `${rows} Knoten sichtbar`;
}

function visibleRows() {
  return Array.from($("tree").querySelectorAll(".row")).filter((r) => r.offsetParent !== null);
}

async function startTree() {
  $("tree-placeholder").hidden = true;
  $("tree").innerHTML = "";
  const rootItem = makeItem(
    { nodeId: ROOT_ID, displayName: "Root", browseName: "0:Root", nodeClass: "Object", hasChildren: true },
    null
  );
  $("tree").appendChild(rootItem);
  await expand(rootItem);
  const objects = rootItem.querySelector(`li[data-node-id="${OBJECTS_ID}"]`);
  if (objects) { await expand(objects); select(objects); }
}

function select(li) {
  for (const row of $("tree").querySelectorAll('.row[aria-selected="true"]')) {
    row.setAttribute("aria-selected", "false");
  }
  const row = li.querySelector(".row");
  row.setAttribute("aria-selected", "true");
  row.scrollIntoView({ block: "nearest" });
  loadDetails(li.dataset.nodeId);
}

/* Tastatursteuerung im Baum */
$("tree-scroll").addEventListener("keydown", async (e) => {
  const keys = ["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Enter", "Home", "End"];
  if (!keys.includes(e.key)) return;
  const rows = visibleRows();
  if (!rows.length) return;
  const current = $("tree").querySelector('.row[aria-selected="true"]');
  let index = rows.indexOf(current);
  e.preventDefault();

  if (e.key === "ArrowDown") index = Math.min(rows.length - 1, index + 1);
  else if (e.key === "ArrowUp") index = Math.max(0, index - 1);
  else if (e.key === "Home") index = 0;
  else if (e.key === "End") index = rows.length - 1;
  else if (e.key === "ArrowRight" && current) {
    const li = current.parentElement;
    if (li.dataset.expanded !== "1") { await expand(li); return; }
    index = Math.min(rows.length - 1, index + 1);
  } else if (e.key === "ArrowLeft" && current) {
    const li = current.parentElement;
    if (li.dataset.expanded === "1") { collapse(li); return; }
    const parent = li.parentElement.closest("li");
    if (parent) { select(parent); return; }
  }
  if (rows[index]) select(rows[index].parentElement);
});
$("tree-scroll").tabIndex = 0;

/* ------------------------------------------------------------------ Details */
async function loadDetails(nodeId) {
  stopPolling();
  S.selectedNode = nodeId;
  S.samples = [];
  $("detail-placeholder").hidden = true;
  $("node-view").hidden = false;
  $("write-note").textContent = "";
  try {
    const node = await api(sp(`/node?nodeId=${encodeURIComponent(nodeId)}`));
    if (S.selectedNode !== nodeId) return;
    renderNode(node);
  } catch (err) {
    status(err.message, "bad");
  }
}

function renderNode(node) {
  S.selectedClass = node.nodeClass;
  $("node-title").textContent = node.displayName || node.browseName;
  $("node-glyph").dataset.class = node.nodeClass;
  $("node-class").textContent = node.nodeClass;
  $("node-id").textContent = node.nodeId;
  $("node-desc").textContent = node.description || "";
  $("node-desc").hidden = !node.description;

  const crumbs = $("crumbs");
  crumbs.innerHTML = "";
  (node.path || []).forEach((step, i) => {
    if (i) crumbs.appendChild(Object.assign(document.createElement("span"), { textContent: "/" }));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = step.name;
    btn.addEventListener("click", () => loadDetails(step.nodeId));
    crumbs.appendChild(btn);
  });

  // Attribute
  const attrs = document.createElement("table");
  attrs.className = "grid";
  attrs.innerHTML = "<thead><tr><th>Attribut</th><th>Wert</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const a of node.attributes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="k">${esc(a.name)}</td><td class="v">${esc(a.text)}</td>`;
    tbody.appendChild(tr);
  }
  attrs.appendChild(tbody);
  $("panel-attributes").innerHTML = "";
  $("panel-attributes").appendChild(attrs);

  // Wert
  const isVariable = node.nodeClass === "Variable";
  $("value-card").hidden = !isVariable;
  $("value-tools").hidden = !isVariable;
  $("write-row").hidden = !(isVariable && node.writable);
  if (isVariable) {
    renderValue(node.value);
    $("write-input").value = node.value && node.value.text ? node.value.text : "";
    if ($("poll-toggle").checked) startPolling();
  } else {
    $("poll-toggle").checked = false;
  }

  const args = $("method-args");
  if (node.nodeClass === "Method" && node.arguments) {
    args.hidden = false;
    args.innerHTML = renderArguments(node.arguments);
  } else {
    args.hidden = true;
  }

  if (!isVariable && S.tab === "value") showTab("attributes");
  if (S.tab === "references") loadReferences(node.nodeId);
  else $("panel-references").innerHTML = "";
}

function renderArguments(argsObj) {
  const section = (title, list) =>
    !list.length ? "" :
    `<h2 class="badge">${esc(title)}</h2><table class="grid"><tbody>` +
    list.map((a) => `<tr><td class="k">${esc(a.name)}</td><td class="v">${esc(a.dataType)}</td></tr>`).join("") +
    "</tbody></table>";
  const html = section("Eingangsargumente", argsObj.input) + section("Ausgangsargumente", argsObj.output);
  return html || '<p class="placeholder">Diese Methode nimmt keine Argumente entgegen.</p>';
}

function renderValue(value) {
  if (!value) return;
  if (value.error) {
    $("value-text").textContent = "nicht lesbar";
    $("value-led").dataset.state = "bad";
    $("value-meta").innerHTML = `<dt>Meldung</dt><dd>${esc(value.error)}</dd>`;
    return;
  }
  $("value-text").textContent = value.text === "" ? '""' : value.text;
  $("value-led").dataset.state = value.status.severity;

  const meta = [
    ["Status", `${value.status.name} (${value.status.code})`],
    ["Datentyp", value.type || "unbekannt"],
    ["Quellzeit", fmtTime(value.sourceTimestamp)],
    ["Serverzeit", fmtTime(value.serverTimestamp)],
  ];
  $("value-meta").innerHTML = meta
    .filter(([, v]) => v)
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("");

  const num = typeof value.value === "number" ? value.value
    : typeof value.value === "boolean" ? (value.value ? 1 : 0) : null;
  if (num !== null) {
    S.samples.push(num);
    if (S.samples.length > 120) S.samples.shift();
  }
  drawTrend();
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("de-DE", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function drawTrend() {
  const svg = $("trend");
  if (S.samples.length < 3) { svg.hidden = true; return; }
  const w = 300, h = 44, pad = 3;
  const min = Math.min(...S.samples), max = Math.max(...S.samples);
  const span = max - min || 1;
  const step = w / Math.max(1, S.samples.length - 1);
  const points = S.samples
    .map((v, i) => `${(i * step).toFixed(1)},${(h - pad - ((v - min) / span) * (h - 2 * pad)).toFixed(1)}`)
    .join(" ");
  svg.hidden = false;
  svg.innerHTML =
    `<line x1="0" y1="${h - pad}" x2="${w}" y2="${h - pad}"></line>` +
    `<polyline points="${points}"></polyline>`;
  svg.setAttribute("aria-label", `Verlauf, ${S.samples.length} Messwerte, zwischen ${min} und ${max}`);
}

/* ------------------------------------------------------------------- Reiter */
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
}

function showTab(name) {
  S.tab = name;
  for (const tab of document.querySelectorAll(".tab")) {
    const on = tab.dataset.tab === name;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", String(on));
  }
  $("panel-value").hidden = name !== "value";
  $("panel-attributes").hidden = name !== "attributes";
  $("panel-references").hidden = name !== "references";
  if (name === "references" && S.selectedNode) loadReferences(S.selectedNode);
}

async function loadReferences(nodeId) {
  const panel = $("panel-references");
  panel.innerHTML = '<p class="placeholder">lade Referenzen …</p>';
  try {
    const data = await api(sp(`/references?nodeId=${encodeURIComponent(nodeId)}`));
    if (S.selectedNode !== nodeId) return;
    if (!data.references.length) {
      panel.innerHTML = '<p class="placeholder">Dieser Knoten hat keine Referenzen.</p>';
      return;
    }
    const table = document.createElement("table");
    table.className = "grid";
    table.innerHTML =
      "<thead><tr><th>Richtung</th><th>Referenztyp</th><th>Ziel</th><th>Klasse</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const ref of data.references) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="dir">${ref.isForward ? "hin" : "zurück"}</td>` +
        `<td>${esc(ref.referenceType)}</td><td></td><td>${esc(ref.nodeClass)}</td>`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ref-link";
      btn.textContent = `${ref.displayName}  ${ref.nodeId}`;
      btn.addEventListener("click", () => loadDetails(ref.nodeId));
      tr.children[2].appendChild(btn);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    panel.innerHTML = "";
    panel.appendChild(table);
  } catch (err) {
    panel.innerHTML = `<p class="placeholder">${esc(err.message)}</p>`;
  }
}

/* ------------------------------------------------------ Werte lesen/schreiben */
async function readValue() {
  if (!S.selectedNode || S.selectedClass !== "Variable") return;
  try {
    const data = await api(sp(`/values?nodeId=${encodeURIComponent(S.selectedNode)}`));
    const value = data.values[S.selectedNode];
    if (value) renderValue(value);
  } catch (err) {
    status(err.message, "bad");
    stopPolling();
    $("poll-toggle").checked = false;
  }
}

function startPolling() {
  stopPolling();
  const interval = Number($("poll-interval").value);
  S.pollTimer = setInterval(readValue, interval);
}

function stopPolling() {
  if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
}

$("poll-toggle").addEventListener("change", (e) => {
  if (e.target.checked) startPolling(); else stopPolling();
});
$("poll-interval").addEventListener("change", () => {
  if ($("poll-toggle").checked) startPolling();
});
$("read-once").addEventListener("click", readValue);

$("write-row").addEventListener("submit", async (e) => {
  e.preventDefault();
  const note = $("write-note");
  note.textContent = "schreibe …";
  note.dataset.state = "";
  try {
    const data = await api(sp("/write"), {
      method: "POST",
      body: JSON.stringify({ nodeId: S.selectedNode, value: $("write-input").value }),
    });
    renderValue(data.value);
    note.textContent = "geschrieben";
    note.dataset.state = "good";
  } catch (err) {
    note.textContent = err.message;
    note.dataset.state = "bad";
  }
});

$("node-id").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("node-id").textContent);
    status("NodeId in die Zwischenablage kopiert.", "good");
  } catch (_) {
    status("Kopieren hat nicht geklappt.", "bad");
  }
});

/* -------------------------------------------------------------------- Suche */
$("search-btn").addEventListener("click", runSearch);
$("search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runSearch(); }
});
$("search").addEventListener("input", () => {
  if (!$("search").value.trim()) {
    $("search-results").hidden = true;
    $("tree").hidden = false;
  }
});

async function runSearch() {
  const q = $("search").value.trim();
  if (q.length < 2) { status("Für die Suche mindestens zwei Zeichen eingeben.", "bad"); return; }
  const box = $("search-results");
  box.hidden = false;
  $("tree").hidden = true;
  box.innerHTML = '<p class="placeholder">durchsuche den Adressraum …</p>';
  try {
    const data = await api(sp(`/search?q=${encodeURIComponent(q)}`));
    box.innerHTML = "";
    if (!data.hits.length) {
      box.innerHTML = `<p class="placeholder">Kein Knoten mit „${esc(q)}“ im Namen. ${data.visited} Knoten durchsucht.</p>`;
      return;
    }
    const back = document.createElement("button");
    back.type = "button";
    back.className = "btn ghost small";
    back.style.margin = "6px 10px";
    back.textContent = "zurück zum Baum";
    back.addEventListener("click", () => { box.hidden = true; $("tree").hidden = false; });
    box.appendChild(back);

    for (const hit of data.hits) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "search-hit";
      item.innerHTML =
        `<span class="hit-name">${esc(hit.displayName)}</span>` +
        `<span class="hit-path">${esc(hit.path.map((p) => p.name).join(" / "))}</span>`;
      item.addEventListener("click", () => reveal(hit.path));
      box.appendChild(item);
    }
    $("tree-foot").textContent = `${data.hits.length} Treffer in ${data.visited} Knoten`;
  } catch (err) {
    box.innerHTML = `<p class="placeholder">${esc(err.message)}</p>`;
  }
}

async function reveal(path) {
  $("search-results").hidden = true;
  $("tree").hidden = false;
  let current = $("tree").querySelector(`li[data-node-id="${cssEscape(ROOT_ID)}"]`);
  if (!current) return;
  for (const step of path) {
    if (step.nodeId === ROOT_ID) continue;
    await expand(current);
    const next = current.querySelector(`:scope > ul > li[data-node-id="${cssEscape(step.nodeId)}"]`);
    if (!next) break;
    current = next;
  }
  select(current);
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, "\\$&");
}

/* ----------------------------------------------------------------- Trenner */
(function splitter() {
  const bar = $("splitter");
  const root = document.documentElement;
  let dragging = false;
  const apply = (px) => root.style.setProperty("--tree-w", `${Math.min(Math.max(px, 220), 900)}px`);

  bar.addEventListener("pointerdown", (e) => { dragging = true; bar.setPointerCapture(e.pointerId); });
  bar.addEventListener("pointermove", (e) => { if (dragging) apply(e.clientX); });
  bar.addEventListener("pointerup", () => { dragging = false; });
  bar.addEventListener("keydown", (e) => {
    const width = parseInt(getComputedStyle(root).getPropertyValue("--tree-w"), 10) || 380;
    if (e.key === "ArrowLeft") apply(width - 20);
    if (e.key === "ArrowRight") apply(width + 20);
  });
})();

/* ------------------------------------------------------------------- Aufbau */
const saved = localStorage.getItem("opcua.url");
if (saved) $("url").value = saved;
setInterval(() => {
  if (S.session) api(sp("/health")).catch(() => {});
}, 60000);
