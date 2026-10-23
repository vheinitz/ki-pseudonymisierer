"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { engines: [], operators: {}, entities: [], lastResult: null,
                sessionKey: null, inputName: null, restoreName: null, rules: [],
                profiles: [], profileDefaults: {}, editingProfile: null };

// Beschriftung und Vorgaben der Presidio-Operatorparameter.
const PARAM_UI = {
  same_id_for_same_value: { label: "gleicher Wert → gleiche ID", type: "checkbox" },
  new_value: { label: "Ersatztext", placeholder: "<ENTITÄT>" },
  masking_char: { label: "Zeichen", value: "*", size: 2 },
  chars_to_mask: { label: "Anzahl", type: "number", value: 100 },
  from_end: { label: "vom Ende", type: "checkbox" },
  hash_type: { label: "Verfahren", options: ["sha256", "sha512"] },
  salt: { label: "Salt", placeholder: "zufällig, wenn leer" },
  key: { label: "Schlüssel (16/24/32 Zeichen)", value: () => randomKey(), size: 34 },
};
const OPERATOR_LABELS = {
  placeholder: "Platzhalter mit ID (umkehrbar)",
  replace: "ersetzen (replace)",
  redact: "entfernen (redact)",
  mask: "maskieren (mask)",
  hash: "hashen (hash)",
  encrypt: "verschlüsseln (encrypt, AES)",
  keep: "beibehalten (keep)",
};

function randomKey() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

// Sitzungsschlüssel für die Platzhalter-IDs (siehe placeholders.py): gleicher Text + gleicher
// Schlüssel = gleiche IDs, damit die Live-Vorschau stabil bleibt. Neuer Schlüssel bei neuem
// Dokument, sonst bekäme dieselbe Person in zwei Dokumenten dieselbe ID.
function newSessionKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  state.sessionKey = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
newSessionKey();

function entityColor(entity) {
  let h = 0;
  for (const c of entity) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 70% 50% / 0.28)`;
}

async function api(path, body, method = "POST") {
  const res = await fetch(path, body === undefined ? {} : {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function upload(file) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/extract", { method: "POST", body: form });
  const data = await res.json().catch(() => ({ error: `${res.status} ${res.statusText}` }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function download(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function derivedName(source, suffix, fallback) {
  if (!source) return fallback;
  const dot = source.lastIndexOf(".");
  const base = dot > 0 ? source.slice(0, dot) : source;
  const ext = dot > 0 ? source.slice(dot).toLowerCase() : "";
  return base + suffix + ([".md", ".markdown", ".txt"].includes(ext) ? ext : ".txt");
}

function showMessage(sel, text) {
  $(sel).textContent = text || "";
  $(sel).hidden = !text;
}

/* ---------- Operator-Editor ---------- */

function operatorEditor(container, initial = "replace") {
  container.innerHTML = "";
  const select = document.createElement("select");
  for (const name of Object.keys(state.operators)) {
    select.add(new Option(OPERATOR_LABELS[name] || name, name));
  }
  select.value = initial;
  const params = document.createElement("span");
  params.className = "params";
  container.append(select, params);

  const renderParams = () => {
    params.innerHTML = "";
    for (const key of Object.keys(state.operators[select.value])) {
      const ui = PARAM_UI[key] || { label: key };
      const label = document.createElement("label");
      label.className = "inline";
      let input;
      if (ui.options) {
        input = document.createElement("select");
        ui.options.forEach((o) => input.add(new Option(o, o)));
      } else {
        input = document.createElement("input");
        input.type = ui.type || "text";
        if (ui.placeholder) input.placeholder = ui.placeholder;
        if (ui.size) input.size = ui.size;
        const v = typeof ui.value === "function" ? ui.value() : ui.value;
        if (v !== undefined) input.value = v;
      }
      input.dataset.param = key;
      label.append(ui.label + " ", input);
      params.append(label);
    }
    scheduleRun();
  };
  select.addEventListener("change", renderParams);
  renderParams();

  return () => {
    const out = { operator: select.value, params: {} };
    params.querySelectorAll("[data-param]").forEach((el) => {
      out.params[el.dataset.param] = el.type === "checkbox" ? el.checked : el.value;
    });
    return out;
  };
}

/* ---------- Engine / Sprache / Entitäten ---------- */

async function loadEngines() {
  const data = await api("/api/engines");
  state.engines = data.engines;
  state.operators = data.operators;
  const sel = $("#engine");
  for (const e of state.engines) {
    const opt = new Option(e.label + (e.languages.length ? "" : " – Modell fehlt"), e.id);
    opt.disabled = !e.languages.length;
    sel.add(opt);
  }
  sel.value = state.engines.find((e) => e.languages.length)?.id;
  state.defaultOperator = operatorEditor($("#default-operator"), "placeholder");
  await onEngineChange();
}

async function onEngineChange() {
  const engine = state.engines.find((e) => e.id === $("#engine").value);
  const langSel = $("#language");
  langSel.innerHTML = "";
  langSel.add(new Option("automatisch", "auto"));
  engine.languages.forEach((l) => langSel.add(new Option(l, l)));
  const fallback = $("#fallback");
  fallback.innerHTML = "";
  engine.languages.forEach((l) => fallback.add(new Option(l, l)));
  if (engine.languages.includes("de")) fallback.value = "de";
  $("#engine-hint").textContent = engine.missing_models.length
    ? `nicht installiert: ${engine.missing_models.join(", ")}` : "";
  await onLanguageChange();
}

async function onLanguageChange() {
  $("#auto-options").hidden = $("#language").value !== "auto";
  $("#entities").innerHTML = '<span class="hint">Modell wird geladen …</span>';
  const data = await api(`/api/entities?engine=${encodeURIComponent($("#engine").value)}&language=${encodeURIComponent($("#language").value)}`);
  state.entities = data.entities;
  const box = $("#entities");
  box.innerHTML = "";
  for (const ent of state.entities) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${ent}" checked> <span class="chip" style="background:${entityColor(ent)}">${ent}</span>`;
    box.append(label);
  }
  document.querySelectorAll("#entity-ops-body select[data-entity]").forEach(fillEntitySelect);
  applyEntityPreset();
  scheduleRun();
}

function fillEntitySelect(sel) {
  const current = sel.value;
  sel.innerHTML = "";
  const custom = customRecognizers().map((c) => c.entity);
  [...new Set([...state.entities, ...custom])].forEach((e) => sel.add(new Option(e, e)));
  if (current) sel.value = current;
}

/* ---------- Eigene Erkenner & Regeln je Entität ---------- */

function addCustomRow() {
  const row = $("#tpl-custom").content.firstElementChild.cloneNode(true);
  row.querySelector("[data-remove]").onclick = () => { row.remove(); scheduleRun(); };
  const kind = row.querySelector('[data-f="kind"]');
  kind.onchange = () => {
    row.querySelector('[data-f="value"]').placeholder =
      kind.value === "regex" ? "\\b[A-Z]{2}\\d{6}\\b" : "Müller, Projekt Phoenix";
  };
  $("#custom-body").append(row);
}

function customRecognizers() {
  return [...document.querySelectorAll("#custom-body tr")].map((row) => {
    const f = (n) => row.querySelector(`[data-f="${n}"]`).value;
    const item = { kind: f("kind"), entity: f("entity").trim().toUpperCase(), score: f("score") };
    if (item.kind === "regex") item.pattern = f("value");
    else item.words = f("value").split(",");
    return item;
  }).filter((c) => c.entity);
}

function addEntityOpRow() {
  const row = document.createElement("tr");
  row.innerHTML = '<td><select data-entity></select></td><td colspan="2" class="operator"></td><td><button class="link" data-remove>✕</button></td>';
  const sel = row.querySelector("select");
  fillEntitySelect(sel);
  sel.onfocus = () => fillEntitySelect(sel);
  row.querySelector("[data-remove]").onclick = () => { row.remove(); scheduleRun(); };
  $("#entity-ops-body").append(row);
  row.getSpec = operatorEditor(row.querySelector(".operator"), "mask");
}

function entityOperators() {
  const out = {};
  document.querySelectorAll("#entity-ops-body tr").forEach((row) => {
    out[row.querySelector("select").value] = row.getSpec();
  });
  return out;
}

/* ---------- Anfrage & Darstellung ---------- */

const lines = (sel) => $(sel).value.split("\n").map((s) => s.trim()).filter(Boolean);

function buildRequest() {
  const checked = [...document.querySelectorAll("#entities input:checked")].map((i) => i.value);
  const custom = customRecognizers();
  // Alle Häkchen gesetzt -> keine Einschränkung, damit auch eigene Erkenner greifen.
  const entities = checked.length === state.entities.length
    ? null : [...checked, ...custom.map((c) => c.entity)];
  return {
    engine: $("#engine").value,
    language: $("#language").value,
    granularity: $("#granularity").value,
    fallback_language: $("#fallback").value,
    text: $("#input").value,
    entities,
    score_threshold: $("#threshold").value,
    allow_list: lines("#allow-list"),
    allow_list_match: $("#allow-list-match").value,
    context: lines("#context").flatMap((l) => l.split(/[,\s]+/)),
    custom_recognizers: custom,
    default_operator: state.defaultOperator(),
    entity_operators: entityOperators(),
    session_key: state.sessionKey,
    apply_rules: $("#apply-rules").checked,
    propagate_names: $("#propagate-names").checked,
    names_only: $("#names-only").checked,
    table_names: $("#table-names").checked,
    profile: $("#profile").value || null,
  };
}

function renderSpans(target, text, spans) {
  target.innerHTML = "";
  let pos = 0;
  for (const s of spans) {
    if (s.start < pos) continue; // überlappende Fundstellen nur einmal markieren
    target.append(text.slice(pos, s.start));
    const mark = document.createElement("mark");
    mark.style.background = entityColor(s.entity_type);
    mark.title = s.title;
    mark.textContent = text.slice(s.start, s.end);
    target.append(mark);
    pos = s.end;
  }
  target.append(text.slice(pos));
}

function render(req, res) {
  state.lastResult = { req, res };
  // Übernommen (gespeichert) -> Hinweis für die KI an den Anfang, damit die Ausgabe komplett
  // kopiert und direkt in die KI eingefügt werden kann. Die Live-Vorschau zeigt nur den Text.
  const prompt = res.persisted ? (res.prompt[$("#prompt-lang").value] || "") : "";
  const prefix = prompt ? `${prompt}\n\n---\n\n` : "";
  renderSpans($("#output"), prefix + res.text, res.items.map((i) => ({
    ...i, start: i.start + prefix.length, end: i.end + prefix.length, title: `${i.entity_type} · ${i.operator}` })));
  renderSpans($("#highlight"), req.text, res.findings.map((f) => ({ ...f, title: `${f.entity_type} · ${f.score}` })));
  $("#count").textContent = res.findings.length;
  $("#findings-body").innerHTML = "";
  for (const f of res.findings) {
    const tr = document.createElement("tr");
    const cells = [f.entity_type, f.text, f.score.toFixed(2), f.recognizer ?? "", f.pattern ?? "", f.context_boost ? "✓" : "", `${f.start}–${f.end}`];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      if (i === 0) td.innerHTML = `<span class="chip" style="background:${entityColor(c)}">${c}</span>`;
      else td.textContent = c;
      tr.append(td);
    });
    $("#findings-body").append(tr);
  }
  $("#decrypt").hidden = !res.items.some((i) => i.operator === "encrypt");
  renderLanguageBlocks(req.text, res.language_blocks);
  renderReleases(res);
  renderPersistStatus(res);
  $("#prompt").value = res.prompt[$("#prompt-lang").value] || "";
  $("#prompt-count").textContent = res.placeholders ? `· ${res.placeholders} Platzhalter` : "";
}

function renderReleases(res) {
  const lines = [];
  const profile = $("#profile").selectedOptions[0]?.textContent;
  if (res.released.length) {
    lines.push(`Bewusst nicht ersetzt (Profil „${profile}“) – bitte prüfen:`);
    res.released.forEach((r) => lines.push(`  • ${r.entry} (${r.count}×): ${r.reason}`));
  }
  if (res.not_names.length) {
    const words = [...new Set(res.not_names.map((n) => n.text))];
    lines.push(`Nicht ersetzt, kein Eigenname (${res.not_names.length}×): ${words.join(", ")}`);
  }
  if (res.release_blocked.length) {
    lines.push("Nicht freigegeben, bleibt ersetzt:");
    res.release_blocked.forEach((r) => lines.push(`  • ${r.entry}: ${r.reason}`));
  }
  showMessage("#release-box", lines.join("\n"));
}

function renderPersistStatus(res) {
  const status = $("#persist-status");
  status.hidden = !res.placeholders;
  status.textContent = res.persisted ? "gespeichert ✓" : "Vorschau – „Anonymisieren“ klicken";
  status.className = "status " + (res.persisted ? "ok" : "pending");
  status.title = res.persisted
    ? "Die Platzhalter lassen sich unter „2. Antwort der KI“ wieder auflösen."
    : "Wird gespeichert, sobald der Text übernommen wird: Anonymisieren, Kopieren oder Speichern.";
}

// Zuordnung speichern, bevor der Text die Seite verlässt. Die Anfrage ist dieselbe wie die
// angezeigte Vorschau; durch den Sitzungsschlüssel ergeben sich dieselben IDs.
async function commit() {
  const last = state.lastResult;
  if (!last || !last.res.placeholders || last.res.persisted) return;
  const res = await api("/api/anonymize", { ...last.req, persist: true });
  if (res.text !== last.res.text) {
    render(last.req, res);
    throw new Error("Ergebnis hat sich beim Speichern geändert – bitte den neuen Text übernehmen.");
  }
  last.res.persisted = true;
  render(last.req, last.res);
}


function renderLanguageBlocks(text, blocks) {
  $("#lang-blocks").hidden = !blocks.length;
  const counts = {};
  blocks.forEach((b) => { counts[b.language] = (counts[b.language] || 0) + 1; });
  $("#main-summary").textContent = blocks.length
    ? "Sprache: " + Object.entries(counts).map(([l, n]) => `${l} ×${n}`).join(", ") : "";
  const body = $("#lang-blocks-body");
  body.innerHTML = "";
  for (const b of blocks) {
    const tr = document.createElement("tr");
    const excerpt = text.slice(b.start, b.end);
    const used = b.language === b.detected ? b.language
      : `${b.language} (${b.reliable ? "kein Modell" : "geglättet"})`;
    [excerpt.length > 70 ? excerpt.slice(0, 70) + " …" : excerpt, b.detected, b.score.toFixed(2), used]
      .forEach((c, i) => {
        const td = document.createElement("td");
        td.textContent = c;
        if (i === 2 && !b.reliable) td.className = "muted";
        tr.append(td);
      });
    body.append(tr);
  }
}

let seq = 0;
async function run(persist = false, throwErrors = false) {
  const my = ++seq;
  const req = buildRequest();
  state.running = (state.running || 0) + 1;
  updateFreshness();
  try {
    const res = await api("/api/anonymize", persist ? { ...req, persist: true } : req);
    if (my !== seq) {
      if (throwErrors) throw new Error("Überholt von einer neueren Anonymisierung – bitte erneut klicken.");
      return null;
    }
    $("#error").hidden = true;
    render(req, res);
    return res;
  } catch (err) {
    if (my === seq) showMessage("#error", err.message);
    if (throwErrors) throw err;
    return null;
  } finally {
    state.running -= 1;
    updateFreshness();
  }
}

/* ---------- Frische der Ausgaben & Knopf-Rückmeldung ---------- */

// Eine Ausgabe ist frisch, wenn sie genau aus der aktuellen Eingabe und den aktuellen
// Einstellungen entstanden ist und gerade nichts läuft. Nur frische Ausgaben werden kopiert –
// sonst landet womöglich der Text von vorher in der KI.
function outputIsFresh() {
  return Boolean(state.lastResult) && !state.running
    && JSON.stringify(buildRequest()) === JSON.stringify(state.lastResult.req);
}

function restoreIsFresh() {
  const last = state.lastRestore;
  return Boolean(last) && !state.restoreRunning
    && last.text === $("#restore-input").value && last.language === $("#restore-language").value;
}

function updateFreshness() {
  const out = $("#output");
  const stale = Boolean(state.lastResult) && !state.running && !outputIsFresh();
  out.classList.toggle("busy", Boolean(state.running));
  out.classList.toggle("stale", stale);
  const status = $("#persist-status");
  if (state.running) {
    status.hidden = false;
    status.textContent = "läuft …";
    status.className = "status pending";
  } else if (stale) {
    status.hidden = false;
    status.textContent = "veraltet – Eingabe geändert";
    status.className = "status warn";
  } else if (state.lastResult) {
    renderPersistStatus(state.lastResult.res);
  }
  const rout = $("#restore-output");
  rout.classList.toggle("busy", Boolean(state.restoreRunning));
  rout.classList.toggle("stale", Boolean(state.lastRestore) && !state.restoreRunning && !restoreIsFresh());
}

async function ensureFreshOutput() {
  clearTimeout(timer);
  if (!outputIsFresh()) await run(true, true);
  else await commit();
  if (!outputIsFresh()) {
    throw new Error("Die Eingabe wurde währenddessen geändert – nichts übernommen. Bitte erneut klicken.");
  }
}

async function ensureFreshRestore() {
  clearTimeout(restoreTimer);
  if (!restoreIsFresh()) await runRestore(true);
  if (!restoreIsFresh()) throw new Error("Die KI-Antwort wurde währenddessen geändert – bitte erneut klicken.");
}

async function writeClipboard(text, element) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch { /* Ersatzweg unten */ }
  getSelection().selectAllChildren(element);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  if (!ok) {
    throw new Error("Zwischenablage nicht verfügbar (Browser-Rechte). Die Ausgabe ist markiert – bitte Strg+C drücken.");
  }
}

// Knopf zeigt: arbeitet (Drehsymbol, gesperrt) -> ✓ Ergebnis (grün) oder ✗ (rot) + Meldung.
async function withFeedback(button, busyLabel, action) {
  if (button.classList.contains("busy")) return;
  const label = button.dataset.label || (button.dataset.label = button.textContent);
  clearTimeout(button.feedbackTimer);
  button.classList.remove("ok", "fail");
  button.classList.add("busy");
  button.disabled = true;
  button.textContent = busyLabel;
  showMessage(button.dataset.errorTarget || "#error", "");
  let failed = false;
  try {
    button.textContent = (await action()) || "✓ Fertig";
    button.classList.add("ok");
    showMessage(button.dataset.errorTarget || "#error", "");
  } catch (err) {
    button.textContent = `✗ ${button.dataset.failLabel || "fehlgeschlagen"}`;
    button.classList.add("fail");
    failed = true;
    showMessage(button.dataset.errorTarget || "#error", err.message);
  } finally {
    button.classList.remove("busy");
    button.disabled = false;
    // Fehler länger stehen lassen als Erfolg – ein übersehenes „nicht kopiert“ ist das Risiko.
    button.feedbackTimer = setTimeout(() => {
      button.classList.remove("ok", "fail");
      button.textContent = label;
    }, failed ? 10000 : 3500);
  }
}

let timer;
function scheduleRun() {
  if (!$("#auto").checked || !state.entities.length) return;
  clearTimeout(timer);
  timer = setTimeout(run, 400);
}

async function decrypt() {
  const { req, res } = state.lastResult;
  const keys = {};
  if (req.default_operator.operator === "encrypt") keys.DEFAULT = req.default_operator.params.key;
  for (const [ent, spec] of Object.entries(req.entity_operators)) {
    if (spec.operator === "encrypt") keys[ent] = spec.params.key;
  }
  try {
    const out = await api("/api/decrypt", { text: res.text, items: res.items, keys });
    $("#output").textContent = out.text;
    $("#decrypt").hidden = true;
  } catch (err) {
    $("#error").textContent = err.message;
    $("#error").hidden = false;
  }
}

/* ---------- Gekoppeltes Scrollen ---------- */

// Relative Position koppeln: Die Ausgabe hat durch Ersetzungen andere Zeilenlängen als die
// Eingabe, gleiche Pixelpositionen würden auseinanderlaufen.
const scrollPanes = ["#input", "#output", "#highlight"].map((sel) => $(sel));
let scrollSource = null;
for (const pane of scrollPanes) {
  pane.addEventListener("scroll", () => {
    if (!$("#sync-scroll").checked) return;
    if (scrollSource && scrollSource !== pane) return; // vom Koppeln ausgelöst
    scrollSource = pane;
    const max = pane.scrollHeight - pane.clientHeight;
    const ratio = max > 0 ? pane.scrollTop / max : 0;
    for (const other of scrollPanes) {
      if (other !== pane) other.scrollTop = ratio * (other.scrollHeight - other.clientHeight);
    }
    requestAnimationFrame(() => { scrollSource = null; });
  });
}

/* ---------- Datei-Upload ---------- */

let uploadTarget = null;
document.querySelectorAll("[data-upload]").forEach((b) => b.addEventListener("click", () => {
  uploadTarget = b.dataset.upload;
  $("#file-input").value = "";
  $("#file-input").click();
}));

$("#file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const anon = uploadTarget === "input";
  const errorSel = anon ? "#error" : "#restore-error";
  try {
    const data = await upload(file);
    if (anon) {
      newSessionKey(); // neues Dokument -> neue IDs
      state.inputName = data.filename;
      $("#input-file").textContent = "· " + data.filename;
      $("#input").value = data.text;
      showMessage("#warnings", data.warnings.join(" "));
      showMessage(errorSel, "");
      run();
    } else {
      state.restoreName = data.filename;
      $("#restore-file").textContent = "· " + data.filename;
      $("#restore-input").value = data.text;
      showMessage(errorSel, data.warnings.join(" "));
      runRestore();
    }
  } catch (err) {
    showMessage(errorSel, `${file.name}: ${err.message}`);
  }
});

/* ---------- Wiederherstellen ---------- */

let restoreSeq = 0;
async function runRestore(throwErrors = false) {
  const my = ++restoreSeq;
  const text = $("#restore-input").value;
  const language = $("#restore-language").value;
  state.restoreRunning = (state.restoreRunning || 0) + 1;
  updateFreshness();
  try {
    const res = await api("/api/restore", { text, language });
    if (my !== restoreSeq) {
      if (throwErrors) throw new Error("Überholt – bitte erneut klicken.");
      return;
    }
    state.lastRestore = { text, language };
    renderSpans($("#restore-output"), res.text,
      res.replaced.map((r) => ({ ...r, title: `${r.entity_type}:${r.id}` })));
    const langs = Object.entries(res.languages).map(([l, n]) => `${l} ×${n}`).join(", ");
    $("#restore-summary").textContent = (res.replaced.length ? `· ${res.replaced.length} ersetzt` : "")
      + (langs ? ` · Sprache: ${langs}` : "");
    const problems = [];
    if (res.unknown.length) {
      problems.push(`Nicht gefunden (verändert oder von der KI erfunden): ${[...new Set(res.unknown)].join(", ")}`);
    }
    for (const [lang, terms] of Object.entries(res.missing_translations)) {
      problems.push(`Keine Übersetzung nach „${lang}“, Original eingesetzt: ${terms.join(", ")}`);
    }
    if (res.type_changed.length) {
      problems.push("Typ verändert, trotzdem ersetzt: " + res.type_changed
        .map((t) => `${t.found}:${t.id} → ${t.stored}`).join(", "));
    }
    showMessage("#restore-problems", problems.join("\n"));
    showMessage("#restore-error", "");
  } catch (err) {
    if (my === restoreSeq) showMessage("#restore-error", err.message);
    if (throwErrors) throw err;
  } finally {
    state.restoreRunning -= 1;
    updateFreshness();
  }
}

let restoreTimer;
$("#restore-input").addEventListener("input", () => {
  updateFreshness();
  clearTimeout(restoreTimer);
  restoreTimer = setTimeout(() => runRestore(), 300);
});
$("#restore-language").addEventListener("change", () => runRestore());
$("#restore-copy").addEventListener("click", (e) => withFeedback(e.currentTarget, "Kopiere …", async () => {
  await ensureFreshRestore();
  const text = $("#restore-output").textContent;
  if (!text.trim()) throw new Error("Ausgabe ist leer – nichts kopiert.");
  await writeClipboard(text, $("#restore-output"));
  return `✓ Kopiert (${text.length} Zeichen)`;
}));
$("#restore-download").addEventListener("click", (e) => withFeedback(e.currentTarget, "Speichere …", async () => {
  await ensureFreshRestore();
  download($("#restore-output").textContent,
           derivedName(state.restoreName, "_wiederhergestellt", "wiederhergestellt.txt"));
  return "✓ Gespeichert";
}));

async function refreshMappingStats() {
  try {
    const s = await api("/api/mappings");
    $("#mapping-stats").textContent = s.count
      ? `${s.count} (älteste vom ${new Date(s.oldest).toLocaleString("de-DE")})` : "keine";
  } catch { /* Anzeige ist nur informativ */ }
}

/* ---------- Tabs ---------- */

function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === name));
  for (const tab of ["anon", "details", "rules", "profiles", "gateway"]) $(`#tab-${tab}`).hidden = tab !== name;
  if (name === "gateway") loadGateway();
  if (name === "profiles") renderProfileForm();
  if (name === "anon") { scheduleRun(); if ($("#restore-input").value) runRestore(); } // Regeln können sich geändert haben
  if (name === "details") refreshMappingStats();
}
document.querySelectorAll("[data-tab]").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", (e) => {
  e.preventDefault();
  showTab(b.dataset.goto);
}));

/* ---------- Ersetzungsliste ---------- */

const RULE_FIELDS = ["enabled", "search", "language", "case_sensitive", "whole_word",
                     "format", "name", "number", "text"];

async function loadRules() {
  const data = await api("/api/rules");
  state.rules = data.rules;
  $("#rules-body").innerHTML = "";
  state.rules.forEach((r) => $("#rules-body").append(ruleRow(r)));
  rulesChanged();
}

function ruleRow(rule = {}) {
  const row = $("#tpl-rule").content.firstElementChild.cloneNode(true);
  row.dataset.id = rule.id || "";
  for (const f of RULE_FIELDS) {
    const el = row.querySelector(`[data-r="${f}"]`);
    if (rule[f] === undefined) continue;
    if (el.type === "checkbox") el.checked = rule[f];
    else el.value = rule[f];
  }
  for (const [lang, term] of Object.entries(rule.translations || {})) addTranslation(row, lang, term);
  row.addEventListener("input", () => { updateRuleRow(row); scheduleRulesSave(); });
  row.addEventListener("change", () => { updateRuleRow(row); scheduleRulesSave(); });
  row.querySelector("[data-add-translation]").onclick = () => addTranslation(row, "", "").querySelector("input").focus();
  row.querySelector("[data-remove-rule]").onclick = () => {
    const what = row.querySelector('[data-r="search"]').value || "diese Regel";
    if (row.dataset.id && !confirm(`„${what}“ löschen? Ältere KI-Antworten mit diesem Ersatz lassen sich danach nicht mehr auflösen. (Alternative: deaktivieren)`)) return;
    row.remove();
    scheduleRulesSave();
  };
  updateRuleRow(row);
  return row;
}

function addTranslation(row, lang, term) {
  const item = document.createElement("span");
  item.className = "translation";
  item.innerHTML = '<input data-t="lang" list="lang-codes" size="3" placeholder="en"><input data-t="term" size="16" placeholder="Übersetzung"><button class="link" title="entfernen">✕</button>';
  item.querySelector('[data-t="lang"]').value = lang;
  item.querySelector('[data-t="term"]').value = term;
  item.querySelector("button").onclick = () => { item.remove(); scheduleRulesSave(); };
  row.querySelector("[data-translations]").append(item);
  return item;
}

function readRule(row) {
  const rule = { id: row.dataset.id || undefined, translations: {} };
  for (const f of RULE_FIELDS) {
    const el = row.querySelector(`[data-r="${f}"]`);
    rule[f] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  row.querySelectorAll(".translation").forEach((t) => {
    const lang = t.querySelector('[data-t="lang"]').value.trim().toLowerCase();
    const term = t.querySelector('[data-t="term"]').value.trim();
    if (lang && term) rule.translations[lang] = term;
  });
  return rule;
}

// Unvollständige Zeilen (noch kein Suchbegriff oder Ersatz) werden nicht gespeichert.
function isDraft(rule) {
  return !rule.search || (rule.format === "tag" ? !rule.name : !rule.text);
}

function updateRuleRow(row) {
  const rule = readRule(row);
  row.querySelectorAll("[data-show]").forEach((el) => { el.hidden = el.dataset.show !== rule.format; });
  row.querySelector("[data-preview]").textContent = rule.format === "tag"
    ? `<${(rule.name || "NAME").toUpperCase()}:${rule.number || "…"}>` : rule.text;
  row.classList.toggle("draft", isDraft(rule));
  // Pflichtfelder markieren, sobald die Zeile begonnen wurde – sonst bleibt unklar, warum
  // sie nicht gespeichert wird.
  const started = rule.search || rule.name || rule.text;
  const missing = { search: !rule.search, name: rule.format === "tag" && !rule.name,
                    text: rule.format === "plain" && !rule.text };
  for (const [f, isMissing] of Object.entries(missing)) {
    row.querySelector(`[data-r="${f}"]`).classList.toggle("missing", Boolean(started && isMissing));
  }
  row.classList.toggle("disabled", !rule.enabled);
}

let rulesTimer;
function scheduleRulesSave() {
  $("#rules-status").textContent = "ungespeichert …";
  $("#rules-status").className = "status pending";
  clearTimeout(rulesTimer);
  rulesTimer = setTimeout(saveRules, 700);
}

async function saveRules() {
  clearTimeout(rulesTimer);
  rulesTimer = null;
  const rows = [...document.querySelectorAll("#rules-body tr")].filter((r) => !isDraft(readRule(r)));
  try {
    const data = await api("/api/rules", { rules: rows.map(readRule) }, "PUT");
    // Server vergibt IDs und freie Nummern; nur diese Felder zurückschreiben, damit die
    // Eingabe nicht durch Neuaufbau der Tabelle den Fokus verliert.
    data.rules.forEach((r, i) => {
      const row = rows[i];
      if (!row) return;
      row.dataset.id = r.id;
      const number = row.querySelector('[data-r="number"]');
      if (r.format === "tag" && !number.value) number.value = r.number;
      updateRuleRow(row);
    });
    state.rules = data.rules;
    const drafts = document.querySelectorAll("#rules-body tr.draft").length;
    if (drafts) {
      $("#rules-status").textContent = `⚠ ${drafts} Regel${drafts > 1 ? "n" : ""} NICHT gespeichert – Pflichtfeld fehlt (rot markiert)`;
      $("#rules-status").className = "status warn";
    } else {
      $("#rules-status").textContent = "gespeichert ✓";
      $("#rules-status").className = "status ok";
    }
    showMessage("#rules-error", "");
    rulesChanged();
  } catch (err) {
    $("#rules-status").textContent = "nicht gespeichert";
    $("#rules-status").className = "status pending";
    showMessage("#rules-error", err.message);
  }
}

function rulesChanged() {
  const active = state.rules.filter((r) => r.enabled).length;
  $("#rules-count").textContent = `(${active} aktiv)`;
  $("#rules-summary").textContent = `· ${state.rules.length} Regeln, ${active} aktiv`;
  // Zielsprachen für die Wiederherstellung: Sprachen der Suchbegriffe und Übersetzungen
  const langs = new Set();
  state.rules.forEach((r) => { langs.add(r.language); Object.keys(r.translations).forEach((l) => langs.add(l)); });
  const sel = $("#restore-language");
  const current = sel.value;
  sel.innerHTML = '<option value="auto">automatisch</option>';
  [...langs].sort().forEach((l) => sel.add(new Option(l, l)));
  sel.value = [...sel.options].some((o) => o.value === current) ? current : "auto";
}

$("#rules-add").addEventListener("click", () => {
  const row = ruleRow();
  $("#rules-body").prepend(row);
  row.querySelector('[data-r="search"]').focus();
});
$("#rules-filter").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll("#rules-body tr").forEach((row) => {
    const hay = [...row.querySelectorAll("input")].map((i) => i.value).join(" ").toLowerCase();
    row.hidden = q && !hay.includes(q);
  });
});
$("#rules-import").addEventListener("click", () => { $("#rules-file").value = ""; $("#rules-file").click(); });
$("#rules-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    if (rulesTimer) await saveRules();
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/rules/import", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    await loadRules();
    $("#rules-status").textContent = `${data.imported} Zeilen importiert ✓`;
    $("#rules-status").className = "status ok";
    showMessage("#rules-error", "");
  } catch (err) {
    showMessage("#rules-error", `${file.name}: ${err.message}`);
  }
});
$("#rules-export").addEventListener("click", async (e) => {
  if (!rulesTimer) return; // nichts Ungespeichertes
  e.preventDefault();
  await saveRules();
  location.href = e.target.href;
});

loadRules().catch((err) => showMessage("#rules-error", err.message));

/* ---------- Profile ---------- */

const LIST_FIELDS = { known_persons: "\n", blocking_cues: "\n", blocking_cues_line: "\n", confirm_labels: "," };
const APPLIES_TO = ["PERSON", "ORGANIZATION", "LOCATION"];

const currentProfile = () => state.profiles.find((p) => p.id === $("#profile").value) || null;

function applyEntityPreset() {
  const off = currentProfile()?.entities_off || [];
  document.querySelectorAll("#entities input").forEach((i) => { i.checked = !off.includes(i.value); });
}

// Voreinstellungen sichtbar in die Steuerfelder übernehmen – danach frei änderbar.
function applyProfilePresets() {
  const threshold = currentProfile()?.score_threshold ?? 0.35;
  $("#threshold").value = threshold;
  $("#threshold-value").textContent = threshold;
  applyEntityPreset();
}

async function loadProfiles() {
  const data = await api("/api/profiles");
  state.profiles = data.profiles;
  state.profileDefaults = data.defaults;
  if (!state.editingProfile && state.profiles.length) state.editingProfile = state.profiles[0].id;
  refreshProfileSelects();
}

function refreshProfileSelects() {
  const sel = $("#profile");
  const current = sel.value;
  sel.innerHTML = '<option value="">Standard</option>';
  state.profiles.forEach((p) => sel.add(new Option(p.name, p.id)));
  sel.value = state.profiles.some((p) => p.id === current) ? current : "";
  const edit = $("#profiles-select");
  edit.innerHTML = "";
  state.profiles.forEach((p) => edit.add(new Option(p.name, p.id)));
  if (state.editingProfile) edit.value = state.editingProfile;
}

function renderProfileForm() {
  const profile = state.profiles.find((p) => p.id === state.editingProfile);
  $("#profile-form").hidden = !profile;
  $("#profiles-copy").disabled = $("#profiles-delete").disabled = !profile;
  if (!profile) return;
  document.querySelectorAll("#profile-form [data-p]").forEach((el) => {
    const v = profile[el.dataset.p];
    const sep = LIST_FIELDS[el.dataset.p];
    el.value = sep ? (v || []).join(sep === "," ? ", " : sep) : (v ?? "");
  });
  const entities = [...new Set([...state.entities, ...profile.entities_off])].sort();
  $("#profile-entities-off").innerHTML = "";
  for (const ent of entities) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${ent}"> <span class="chip" style="background:${entityColor(ent)}">${ent}</span>`;
    label.querySelector("input").checked = profile.entities_off.includes(ent);
    $("#profile-entities-off").append(label);
  }
  $("#profile-applies-to").innerHTML = "";
  for (const ent of APPLIES_TO) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${ent}"> ${ent}`;
    label.querySelector("input").checked = profile.applies_to.includes(ent);
    $("#profile-applies-to").append(label);
  }
}

function readProfileForm(profile) {
  document.querySelectorAll("#profile-form [data-p]").forEach((el) => {
    const sep = LIST_FIELDS[el.dataset.p];
    profile[el.dataset.p] = sep ? el.value.split(sep).map((x) => x.trim()).filter(Boolean) : el.value;
  });
  profile.score_threshold = profile.score_threshold === "" ? null : profile.score_threshold;
  profile.entities_off = [...document.querySelectorAll("#profile-entities-off input:checked")].map((i) => i.value);
  profile.applies_to = [...document.querySelectorAll("#profile-applies-to input:checked")].map((i) => i.value);
}

let profilesTimer;
function scheduleProfilesSave() {
  $("#profiles-status").textContent = "ungespeichert …";
  $("#profiles-status").className = "status pending";
  clearTimeout(profilesTimer);
  profilesTimer = setTimeout(saveProfiles, 700);
}

async function saveProfiles() {
  clearTimeout(profilesTimer);
  try {
    const data = await api("/api/profiles", { profiles: state.profiles }, "PUT");
    state.profiles = data.profiles;
    if (!state.profiles.some((p) => p.id === state.editingProfile)) {
      // neu angelegtes Profil hat jetzt eine ID: über die Position wiederfinden
      state.editingProfile = state.profiles[state.profiles.length - 1]?.id || null;
    }
    refreshProfileSelects();
    $("#profiles-status").textContent = "gespeichert ✓";
    $("#profiles-status").className = "status ok";
    showMessage("#profiles-error", "");
  } catch (err) {
    $("#profiles-status").textContent = "nicht gespeichert";
    $("#profiles-status").className = "status pending";
    showMessage("#profiles-error", err.message);
  }
}

function newProfile(base) {
  const d = state.profileDefaults;
  const profile = base ? { ...structuredClone(base), id: undefined, name: `${base.name} (Kopie)` } : {
    name: `Neues Profil ${state.profiles.length + 1}`, description: "", score_threshold: null,
    entities_off: [], applies_to: ["PERSON"], known_persons: [], confirm_labels: d.confirm_labels,
    confirm_threshold: 0.5, blocking_cues: d.blocking_cues, blocking_cues_line: d.blocking_cues_line,
    cue_window: 60,
  };
  state.profiles.push(profile);
  state.editingProfile = undefined; // nach dem Speichern: letztes Profil
  saveProfiles().then(renderProfileForm);
}

$("#profile-form").addEventListener("input", (e) => {
  const profile = state.profiles.find((p) => p.id === state.editingProfile);
  if (!profile) return;
  readProfileForm(profile);
  scheduleProfilesSave();
});
$("#profile-form").addEventListener("change", (e) => $("#profile-form").dispatchEvent(new Event("input")));
$("#profiles-select").addEventListener("change", async (e) => {
  if (profilesTimer) await saveProfiles();
  state.editingProfile = e.target.value;
  renderProfileForm();
});
$("#profiles-new").addEventListener("click", () => newProfile());
$("#profiles-copy").addEventListener("click", () => newProfile(state.profiles.find((p) => p.id === state.editingProfile)));
$("#profiles-delete").addEventListener("click", async () => {
  const profile = state.profiles.find((p) => p.id === state.editingProfile);
  if (!profile || !confirm(`Profil „${profile.name}“ löschen?`)) return;
  state.profiles = state.profiles.filter((p) => p !== profile);
  state.editingProfile = state.profiles[0]?.id || null;
  await saveProfiles();
  renderProfileForm();
});
$("#profile").addEventListener("change", () => { applyProfilePresets(); scheduleRun(); });

loadProfiles().catch((err) => showMessage("#profiles-error", err.message));

/* ---------- Verdrahtung ---------- */

$("#engine").addEventListener("change", onEngineChange);
$("#language").addEventListener("change", onLanguageChange);
$("#granularity").addEventListener("change", scheduleRun);
$("#fallback").addEventListener("change", scheduleRun);
$("#run").addEventListener("click", (e) => withFeedback(e.currentTarget, "Anonymisiere …", async () => {
  clearTimeout(timer);
  await run(true, true);
  return "✓ Anonymisiert";
}));
$("#decrypt").addEventListener("click", decrypt);
$("#copy").addEventListener("click", (e) => withFeedback(e.currentTarget, "Kopiere …", async () => {
  await ensureFreshOutput();
  const text = $("#output").textContent;
  if (!text.trim()) throw new Error("Ausgabe ist leer – nichts kopiert.");
  await writeClipboard(text, $("#output"));
  return `✓ Kopiert (${text.length} Zeichen)`;
}));
$("#download").addEventListener("click", (e) => withFeedback(e.currentTarget, "Speichere …", async () => {
  await ensureFreshOutput();
  download($("#output").textContent, derivedName(state.inputName, "_anonymisiert", "anonymisiert.txt"));
  return "✓ Gespeichert";
}));
$("#prompt-lang").addEventListener("change", () => {
  if (state.lastResult) render(state.lastResult.req, state.lastResult.res);
});
// Strg+A in einer Ausgabe markiert nur deren Inhalt, nicht die ganze Seite.
for (const sel of ["#output", "#restore-output"]) {
  $(sel).addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
      e.preventDefault();
      getSelection().selectAllChildren(e.currentTarget);
    }
  });
}
// Markieren + Strg+C in der Ausgabe nur, wenn sie frisch und übernommen ist – sonst würde ein
// veralteter Text oder eine Vorschau ohne Hinweis für die KI in die Zwischenablage gelangen.
$("#output").addEventListener("copy", (e) => {
  if (outputIsFresh() && state.lastResult.res.persisted) return;
  e.preventDefault();
  showMessage("#error", outputIsFresh()
    ? "Nicht kopiert: Das ist nur die Vorschau. Bitte „Alles kopieren“ benutzen – dann wird übernommen und der Hinweis für die KI vorangestellt."
    : "Nicht kopiert: Die Ausgabe ist veraltet. Bitte „Alles kopieren“ benutzen – dann wird neu anonymisiert.");
});
$("#restore-output").addEventListener("copy", (e) => {
  if (restoreIsFresh()) return;
  e.preventDefault();
  showMessage("#restore-error", "Nicht kopiert: Die Ausgabe passt nicht mehr zur KI-Antwort. Bitte „Alles kopieren“ benutzen.");
});
$("#new-ids").addEventListener("click", () => { newSessionKey(); run(); });
// Gesamten Inhalt durch Einfügen ersetzt -> neues Dokument -> neue IDs.
$("#input").addEventListener("paste", (e) => {
  const t = e.target;
  if (t.selectionStart === 0 && t.selectionEnd === t.value.length) {
    newSessionKey();
    state.inputName = null;
    $("#input-file").textContent = "";
  }
});
$("#add-custom").addEventListener("click", addCustomRow);
$("#add-entity-op").addEventListener("click", addEntityOpRow);
$("#threshold").addEventListener("input", (e) => { $("#threshold-value").textContent = e.target.value; });
document.querySelectorAll("[data-select]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#entities input").forEach((i) => { i.checked = b.dataset.select === "all"; });
  scheduleRun();
}));
// Jede Änderung in Eingabe oder Steuerfeldern löst (verzögert) eine neue Anonymisierung aus.
const NO_RERUN = ["auto", "sync-scroll", "prompt-lang", "prompt", "restore-input", "restore-language", "deny-list",
                  "agent", "agent-session"];
$("#apply-rules").addEventListener("change", scheduleRun);
$("#propagate-names").addEventListener("change", scheduleRun);
$("#names-only").addEventListener("change", scheduleRun);
for (const tab of ["#tab-anon", "#tab-details"]) {
  $(tab).addEventListener("input", (e) => { updateFreshness(); if (!NO_RERUN.includes(e.target.id)) scheduleRun(); });
  $(tab).addEventListener("change", (e) => { updateFreshness(); if (!NO_RERUN.includes(e.target.id)) scheduleRun(); });
}

loadEngines().catch((err) => {
  $("#error").textContent = err.message;
  $("#error").hidden = false;
});

/* ---------- Gateway-Protokoll ---------- */

const BLOCK_KINDS = {
  system: "Systemprompt", system_text: "Systemkontext", user_text: "Eingabe",
  assistant_text: "frühere Antwort", tool_use: "Werkzeugaufruf", tool_result: "Werkzeugergebnis",
  thinking: "Denkprozess", response_text: "Antwort", response_tool_use: "Werkzeugaufruf (Antwort)",
  tool_definition: "Werkzeugdefinition", other: "sonstiges",
};
const SYSTEM_KINDS = ["system", "system_text", "tool_definition"];

async function loadGateway() {
  try {
    state.gateway = await api("/api/gateway/overview");
    showMessage("#gw-error", "");
  } catch (err) {
    showMessage("#gw-error", err.message);
    return;
  }
  const { status, summary, gateway_url } = state.gateway;
  $("#gw-url").textContent = gateway_url;
  $("#gw-status").textContent = status
    ? `läuft · Protokoll ${status.logging ? "an" : "aus"} · ${status.exchanges} Anfragen seit Start`
    : "nicht erreichbar";
  $("#gw-status").className = "status " + (status ? (status.logging ? "ok" : "pending") : "warn");
  $("#gw-summary").textContent = `· ${summary.exchanges} Austausche, ${summary.analyzed}/${summary.blocks} Blöcke analysiert`;
  const types = [...new Set(state.gateway.findings.map((f) => f.entity_type))].sort();
  const sel = $("#gw-type");
  const current = sel.value;
  sel.innerHTML = '<option value="">alle</option>';
  types.forEach((t) => sel.add(new Option(t, t)));
  sel.value = types.includes(current) ? current : "";
  renderGatewayFindings();
  const body = $("#gw-exchanges");
  body.innerHTML = "";
  for (const e of state.gateway.exchanges) {
    const tr = document.createElement("tr");
    const u = e.usage || {};
    const tokensIn = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    [e.id, new Date(e.started_at).toLocaleString("de-DE"), e.path, e.status + (e.error ? " ⚠" : ""),
     e.model || "", `${(e.duration_ms / 1000).toFixed(1)} s`, e.new_blocks,
     e.usage ? `${tokensIn} / ${u.output_tokens || 0}` : ""].forEach((c) => {
      const td = document.createElement("td");
      td.textContent = c;
      tr.append(td);
    });
    if (e.error) tr.title = e.error;
    body.append(tr);
  }
}

function renderGatewayFindings() {
  if (!state.gateway) return;
  const type = $("#gw-type").value;
  const hideSystem = $("#gw-hide-system").checked;
  const minScore = parseFloat($("#gw-min-score").value) || 0;
  const q = $("#gw-filter").value.toLowerCase();
  const body = $("#gw-findings");
  body.innerHTML = "";
  for (const f of state.gateway.findings) {
    const kinds = hideSystem ? f.kinds.filter((k) => !SYSTEM_KINDS.includes(k)) : f.kinds;
    if ((type && f.entity_type !== type) || !kinds.length || f.max_score < minScore
        || (q && !f.text.toLowerCase().includes(q))) continue;
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const cells = [f.entity_type, f.text, f.max_score.toFixed(2), f.sent,
                   kinds.map((k) => BLOCK_KINDS[k] || k).join(", "),
                   new Date(f.first_at).toLocaleString("de-DE")];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      if (i === 0) td.innerHTML = `<span class="chip" style="background:${entityColor(c)}">${c}</span>`;
      else td.textContent = c;
      tr.append(td);
    });
    const example = f.examples.find((x) => kinds.includes(x.kind)) || f.examples[0];
    tr.onclick = () => showGatewayBlock(example);
    body.append(tr);
  }
}

async function showGatewayBlock(example) {
  const block = await api(`/api/gateway/block/${example.hash}`);
  $("#gw-block").hidden = false;
  $("#gw-block-kind").textContent = `· ${BLOCK_KINDS[block.kind] || block.kind} · ${block.content.length} Zeichen`;
  // Ausschnitt um die Fundstelle, alle Fundstellen darin markiert
  const from = Math.max(0, example.start - 600);
  const to = Math.min(block.content.length, example.end + 600);
  const spans = block.findings.filter((f) => f.start >= from && f.end <= to)
    .map((f) => ({ ...f, start: f.start - from, end: f.end - from, title: `${f.entity_type} · ${f.score}` }))
    .sort((a, b) => a.start - b.start);
  renderSpans($("#gw-block-text"), (from ? "… " : "") + block.content.slice(from, to), spans.map((sp) =>
    from ? { ...sp, start: sp.start + 2, end: sp.end + 2 } : sp));
  $("#gw-block").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

["#gw-type", "#gw-hide-system", "#gw-min-score", "#gw-filter"].forEach((sel) =>
  $(sel).addEventListener("input", renderGatewayFindings));
$("#gw-refresh").addEventListener("click", loadGateway);
for (const [btn, st] of [["#gw-log-on", "on"], ["#gw-log-off", "off"]]) {
  $(btn).addEventListener("click", async () => {
    try {
      await api(`/api/gateway/logging/${st}`, {});
      loadGateway();
    } catch (err) {
      showMessage("#gw-error", err.message);
    }
  });
}

/* ---------- Deny-List ---------- */

async function loadDenyList() {
  const data = await api("/api/deny-list");
  $("#deny-list").value = data.lines.join("\n");
  $("#deny-count").textContent = data.lines.length ? `· ${data.lines.length} Einträge` : "";
}

let denyTimer;
$("#deny-list").addEventListener("input", () => {
  $("#deny-status").textContent = "ungespeichert …";
  $("#deny-status").className = "status pending";
  clearTimeout(denyTimer);
  denyTimer = setTimeout(async () => {
    try {
      const data = await api("/api/deny-list", { lines: $("#deny-list").value.split("\n") }, "PUT");
      $("#deny-count").textContent = data.lines.length ? `· ${data.lines.length} Einträge` : "";
      $("#deny-status").textContent = "gespeichert ✓";
      $("#deny-status").className = "status ok";
      // Liste ist serverseitig: Ausgabe ist damit veraltet und wird beim Kopieren neu erzeugt
      if (state.lastResult) state.lastResult.req = { ...state.lastResult.req, _deny: Date.now() };
      updateFreshness();
      scheduleRun();
    } catch (err) {
      $("#deny-status").textContent = `nicht gespeichert: ${err.message}`;
      $("#deny-status").className = "status warn";
    }
  }, 600);
});
loadDenyList().catch(() => {});

/* ---------- KI-Anfrage an lokale Agenten ---------- */

async function loadAgents() {
  const data = await api("/api/agents");
  state.agents = data.agents;
  state.agentSessions = data.sessions;
  const sel = $("#agent");
  const current = sel.value;
  sel.innerHTML = "";
  for (const a of state.agents) {
    const opt = new Option(a.label + (a.available ? "" : " – nicht installiert"), a.id);
    opt.disabled = !a.available;
    sel.add(opt);
  }
  sel.value = state.agents.some((a) => a.id === current) ? current
    : (state.agents.find((a) => a.available) || {}).id || "";
  fillAgentSessions();
}

function fillAgentSessions(selectId) {
  const sel = $("#agent-session");
  const current = selectId || sel.value || "new";
  sel.innerHTML = "";
  sel.add(new Option("neue Sitzung", "new"));
  sel.add(new Option("ohne Sitzung (nichts speichern)", "none"));
  const sessions = (state.agentSessions || []).filter((x) => x.agent === $("#agent").value);
  if (sessions.length) {
    const group = document.createElement("optgroup");
    group.label = "fortsetzen";
    for (const x of sessions) {
      const when = new Date(x.last_used).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
      group.append(new Option(`${when} · ${x.turns}× · ${x.title}`, x.id));
    }
    sel.append(group);
  }
  sel.value = [...sel.options].some((o) => o.value === current) ? current : "new";
}

$("#agent").addEventListener("change", () => fillAgentSessions("new"));

$("#ask-agent").addEventListener("click", (e) => withFeedback(e.currentTarget, "KI arbeitet …", async () => {
  // Nur frische, übernommene Ausgabe (mit Hinweis für die KI) senden – wie beim Kopieren.
  await ensureFreshOutput();
  const text = $("#output").textContent;
  if (!text.trim()) throw new Error("Ausgabe ist leer – erst anonymisieren.");
  const agent = $("#agent").value;
  const session = $("#agent-session").value;
  const t0 = Date.now();
  const tick = setInterval(() => {
    $("#agent-status").textContent = `wartet auf Antwort … ${Math.round((Date.now() - t0) / 1000)} s`;
  }, 1000);
  $("#agent-status").className = "status pending";
  try {
    const res = await api("/api/agents/ask", { agent, session, text });
    const input = $("#restore-input");
    input.value = res.answer;
    state.restoreName = null;
    $("#restore-file").textContent = `· von ${$("#agent").selectedOptions[0].textContent}`;
    await runRestore(true);
    await loadAgents();
    // Nach neuer Sitzung direkt diese zum Fortsetzen vorwählen
    fillAgentSessions(res.mode === "none" ? "none" : res.session_id);
    $("#agent-status").textContent = `Antwort nach ${res.duration_s} s`
      + (res.mode === "none" ? " · ohne Sitzung" : res.mode === "new" ? " · neue Sitzung angelegt" : " · Sitzung fortgesetzt");
    $("#agent-status").className = "status ok";
    input.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return "✓ Antwort erhalten";
  } catch (err) {
    $("#agent-status").textContent = "";
    throw err;
  } finally {
    clearInterval(tick);
  }
}));

loadAgents().catch((err) => showMessage("#agent-error", err.message));
