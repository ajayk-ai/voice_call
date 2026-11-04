"use strict";
/* SCM user app: 1 Vendor Master, 2 Vendor Control, 3 Trace, 4 Call Results. */
const session = Session.require("scm");
const api = (path, opts) => request("dashboard/api" + path, opts);
const send = (path, body, method) => post("dashboard/api" + path, body, method);
if (session) $("me").textContent = session.name;

const TABS = ["vendors", "control", "trace", "calls"];
const PAGE_SIZE = 25;
const SCENARIOS = ["Scenario 1", "Scenario 2", "Scenario 3", "Scenario 4"];
const STEP_TEXT = [["WhatsApp alert", "Vendor planner and coordinator"], ["WhatsApp escalation", "Vendor management"], ["Voice agent call", "Vendor, with a task for the buyer"], ["Management meeting", "Buyer, SCM head and vendor"]];
const REASON = { material_shortage: "Material shortage", capacity: "Capacity / machine", quality_issue: "Quality issue", payment: "Payment / dues",
  logistics: "Logistics / transport", labour: "Labour", design_change: "Design change", awaiting_information: "Awaiting information", other: "Other", none: "—" };
const LINE_STATUS = { dispatched: "Dispatched", ready_to_dispatch: "Ready to dispatch", in_production: "In production", not_started: "Not started", unknown: "Not said" };
let tab = "vendors", refreshTimer = null;
let vendors = null;  // GET /vendor-numbers
const V = { q: "", filter: "all", page: 0 };
let ctl = null;  // GET /control
const C = { scope: "selected", picks: new Set(), q: "", overdue: true, ready: false, dirty: false, mode: "dry", sched: "interval", formReady: false };
const T = { list: null, q: "", level: "any", limit: 50, pg: "", buyer: "", age: "", call: "", delivery: "" };
const L = { list: null, insights: null };
let stopped = false;  // the emergency stop is on

// ---------- tabs ----------
function showTab(t) {
  if (!TABS.includes(t)) t = "vendors";
  tab = t;
  for (const x of TABS) $("tab-" + x).hidden = x !== t;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t));
  history.replaceState(null, "", "#" + t);
  clearInterval(refreshTimer);
  refresh();
  refreshTimer = setInterval(() => { if (!document.hidden && t !== "vendors" && $("drawer").hidden) refresh(); }, 30000);
}
function refresh() {
  if (tab === "vendors") loadVendors();
  else if (tab === "control") { loadControl(); if (!vendors) loadVendors(); else renderPicker(); }
  else if (tab === "trace") loadTrace();
  else loadCalls();
}
document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

// ---------- emergency stop ----------
function renderEmergency(c) {
  stopped = c.emergency_stop;
  $("eBanner").hidden = !stopped; $("eStop").hidden = stopped;
  if (stopped) $("eText").textContent = `Emergency stop is on${c.emergency_stop_by ? ` (pressed by ${c.emergency_stop_by}${c.emergency_stop_at ? `, ${fmtDateTime(c.emergency_stop_at)}` : ""})` : ""}. No messages or calls go out until it is cleared.`;
}
const checkEmergency = () => api("/control").then(renderEmergency).catch(() => {});
$("eStop").onclick = async () => {
  if (!(await ask("Emergency stop?", "The run in progress stops, ringing and connected calls are hung up, and nothing goes out until someone clears this.", "Stop everything"))) return;
  try {
    const c = await send("/control/emergency-stop");
    renderEmergency(c); if (ctl) { ctl = { ...ctl, ...c }; renderControl(); }
    toast(`Stopped. Calls hung up: ${c.calls_ended}${c.calls_not_ended ? `, could not end: ${c.calls_not_ended} (they may already be over)` : ""}`, Boolean(c.calls_not_ended));
  } catch (e) { toast(`Emergency stop FAILED: ${e.message}`, true); }
};
$("eClear").onclick = async () => {
  if (!(await ask("Clear the emergency stop?", "Runs are allowed again. The automatic follow-up stays off until you press Start.", "Clear"))) return;
  try { const c = await send("/control/emergency-clear"); renderEmergency(c); if (ctl) { ctl = { ...ctl, ...c }; renderControl(); } toast("Emergency stop cleared"); }
  catch (e) { toast(e.message, true); }
};
if (session) { checkEmergency(); setInterval(() => { if (!document.hidden) checkEmergency(); }, 15000); }

// ---------- 1 Vendor Master ----------
const parts = (s) => String(s || "").split(/[,;/]/).map((x) => x.trim()).filter(Boolean);
const isIndian = (raw) => /^(?:91|0)?[6-9]\d{9}$/.test(String(raw).replace(/\D/g, ""));
const okNumbers = (s) => parts(s).every(isIndian);
const usedNumber = (v, i) => v.numbers[i] || (i === 2 ? v.numbers[1] || v.fallback[1] : v.fallback[i]) || "";
function vendorStatus(v) {
  const used = [0, 1, 2].map((i) => usedNumber(v, i));
  if (used.some((u) => u && !okNumbers(u))) return { key: "wrong", label: "Wrong number", cls: "bad" };
  if (used.some((u) => !u)) return { key: "missing", label: "No number", cls: "warn" };
  if (!v.emails) return { key: "missing", label: "No e-mail", cls: "warn" };
  return { key: "ready", label: "Ready", cls: "ok" };
}
async function loadVendors() {
  try { vendors = await api("/vendor-numbers"); } catch (e) {
    $("vBody").innerHTML = `<tr><td colspan="8" class="empty"><b>Could not load vendors</b>${esc(e.message)}</td></tr>`; return;
  }
  if (!C.ready && ctl) initPicker();
  renderVendors(); renderPicker();
}
function renderVendors() {
  const all = vendors.vendors, st = all.map(vendorStatus);
  const count = (k) => st.filter((s) => s.key === k).length;
  const tiles = [["all", "All vendors", all.length], ["ready", "Ready", count("ready")], ["missing", "Missing details", count("missing")], ["wrong", "Wrong number", count("wrong")], ["overdue", "With overdue lines", all.filter((v) => v.overdue_lines > 0).length]];
  $("vTiles").innerHTML = tiles.map(([k, l, c]) => `<button class="tile" data-vf="${k}" aria-pressed="${V.filter === k}"><span>${l}</span><b>${num(c)}</b></button>`).join("");
  const q = V.q.trim().toLowerCase();
  const rows = all.filter((v) => (V.filter === "all" || (V.filter === "overdue" ? v.overdue_lines > 0 : vendorStatus(v).key === V.filter))
    && (!q || [v.code, v.name, ...v.numbers, v.emails].join(" ").toLowerCase().includes(q)));
  V.page = Math.min(V.page, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
  const shown = rows.slice(V.page * PAGE_SIZE, (V.page + 1) * PAGE_SIZE);
  const numCell = (v, i) => {
    const own = v.numbers[i], u = usedNumber(v, i);
    if (!u) return `<span class="muted">—</span>`;
    return `<span class="mono ${okNumbers(u) ? (own ? "" : "from-master") : "wrong"}" title="${own ? "" : i === 2 ? "Same as Scenario 2" : "From the PO Master"}">${esc(u)}</span>`;
  };
  $("vBody").innerHTML = shown.length ? shown.map((v) => {
    const s = vendorStatus(v);
    return `<tr><td><div class="name" title="${esc(v.name)}">${esc(v.name)}</div><div class="sub">${esc(v.code)}</div></td><td class="r">${num(v.overdue_lines)}</td>
      <td>${numCell(v, 0)}</td><td>${numCell(v, 1)}</td><td>${numCell(v, 2)}</td><td><div class="emails" title="${esc(v.emails)}">${esc(v.emails) || `<span class="muted">—</span>`}</div></td>
      <td><span class="pill ${s.cls}">${s.label}</span></td><td class="r"><button class="sm" data-edit="${esc(v.code)}">Edit</button></td></tr>`;
  }).join("") : `<tr><td colspan="8" class="empty"><b>No vendors here</b>${all.length ? "Try another search or filter." : "Add a vendor or upload your vendor master."}</td></tr>`;
  $("vRange").textContent = rows.length ? `${num(V.page * PAGE_SIZE + 1)}–${num(V.page * PAGE_SIZE + shown.length)} of ${num(rows.length)}` : "";
  $("vPrev").disabled = V.page === 0; $("vNext").disabled = (V.page + 1) * PAGE_SIZE >= rows.length;
}
$("vTiles").onclick = (e) => { const b = e.target.closest("[data-vf]"); if (b) { V.filter = b.dataset.vf; V.page = 0; renderVendors(); } };
$("vQ").oninput = (e) => { V.q = e.target.value; V.page = 0; if (vendors) renderVendors(); };
$("vPrev").onclick = () => { V.page--; renderVendors(); };
$("vNext").onclick = () => { V.page++; renderVendors(); };
$("vBody").onclick = (e) => { const b = e.target.closest("[data-edit]"); if (b) openVendor(b.dataset.edit); };
$("vAdd").onclick = () => openVendor(null);

let editing = null;
function openVendor(code) {
  const v = code ? vendors.vendors.find((x) => x.code === code) : null;
  editing = code;
  $("vTitle").textContent = v ? "Edit vendor" : "Add vendor";
  $("fCode").value = v?.code || ""; $("fCode").readOnly = !!v; $("fName").value = v?.name || "";
  [0, 1, 2].forEach((i) => { $("fN" + (i + 1)).value = v?.numbers[i] || ""; $("fN" + (i + 1)).placeholder = (v && (i === 2 ? v.numbers[1] || v.fallback[1] : v.fallback[i])) || "9876543210"; });
  $("fMail").value = v?.emails || "";
  ["eN1", "eN2", "eN3", "eMail"].forEach((id) => { $(id).textContent = ""; }); $("vErr").hidden = true;
  ["fN1", "fN2", "fN3", "fMail"].forEach((id) => $(id).classList.remove("bad"));
  $("vDlg").showModal();
  (v ? $("fN1") : $("fCode")).focus();
}
function checkNumberField(i) {
  const el = $("fN" + i), bad = parts(el.value).filter((p) => !isIndian(p));
  el.classList.toggle("bad", bad.length > 0);
  $("eN" + i).textContent = bad.length ? `${bad[0]} is not a 10-digit mobile number` : "";
  return !bad.length;
}
[1, 2, 3].forEach((i) => { $("fN" + i).onblur = () => checkNumberField(i); $("fN" + i).oninput = () => { if ($("fN" + i).classList.contains("bad")) checkNumberField(i); }; });
$("vForm").onsubmit = async (e) => {
  e.preventDefault(); $("vErr").hidden = true;
  const code = $("fCode").value.trim(), name = $("fName").value.trim();
  const fail = (m) => { $("vErr").textContent = m; $("vErr").hidden = false; };
  if (!code) return fail("Enter the vendor code.");
  if (!editing && !name && !vendors.vendors.some((v) => v.code === code)) return fail("Enter the vendor name.");
  if (![1, 2, 3].map(checkNumberField).every(Boolean)) return;
  $("vSave").disabled = true;
  try {
    await send("/vendor-numbers/" + encodeURIComponent(code), { name: name || null, numbers: [$("fN1").value, $("fN2").value, $("fN3").value], emails: $("fMail").value }, "PUT");
    $("vDlg").close(); toast("Vendor saved"); loadVendors();
  } catch (err) { fail(err.message); } finally { $("vSave").disabled = false; }
};
$("vExport").onclick = () => {
  if (!vendors) return;
  saveCsv("vendor-master.csv", [["Vendor Code", "Vendor Name", "Scenario 1", "Scenario 2", "Scenario 3", "Scenario 4 Emails", "Overdue Lines", "Status"],
    ...vendors.vendors.map((v) => [v.code, v.name, ...v.numbers, v.emails, v.overdue_lines, vendorStatus(v).label])]);
};

// upload
let uploadRows = null;
function parseCsv(text) {
  const rows = []; let row = [], cell = "", quoted = false;
  text = text.replace(/^﻿/, "");
  const delim = (text.split(/\r?\n/, 1)[0].match(/;/g) || []).length > (text.split(/\r?\n/, 1)[0].match(/,/g) || []).length ? ";" : ",";
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false; } else cell += c; }
    else if (c === '"') quoted = true;
    else if (c === delim) { row.push(cell); cell = ""; }
    else if (c === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (c !== "\r") cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((x) => x.trim()));
}
function toUploadRows(table) {
  const key = (h) => h.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const head = table[0].map(key);
  const find = (...names) => head.findIndex((h) => names.includes(h));
  const iCode = find("vendor code", "code"), iName = find("vendor name", "name");
  const iNum = [1, 2, 3].map((n) => find(`scenario ${n}`, `scenario ${n} no`, `scenario ${n} number`, `scenario ${n} mobile`));
  const iMail = find("scenario 4 emails", "scenario 4 email", "scenario 4", "emails", "email", "e mail");
  if (iCode < 0) throw new Error("The file needs a 'Vendor Code' column.");
  const val = (r, i) => { if (i < 0) return null; const v = (r[i] || "").trim().replace(/^="?|"$/g, ""); return v === "" ? null : v; };
  return table.slice(1).map((r, n) => ({ line: n + 2, code: val(r, iCode) || "", name: val(r, iName), numbers: iNum.map((i) => val(r, i)), emails: val(r, iMail) }));
}
async function readUpload(file) {
  $("uSave").disabled = true; uploadRows = null;
  const box = $("uResult");
  if (/\.xlsx?$/i.test(file.name)) { box.innerHTML = `<div class="note bad">Excel files cannot be read directly. In Excel use Save As, CSV, then upload that file.</div>`; return; }
  box.innerHTML = `<p class="muted small">Checking ${esc(file.name)}…</p>`;
  try {
    const table = parseCsv(await file.text());
    if (table.length < 2) throw new Error("The file has no vendor rows.");
    const rows = toUploadRows(table);
    const r = await send("/vendor-numbers/bulk", { rows, apply: false });
    uploadRows = rows; $("uSave").disabled = false;
    box.innerHTML = `<div class="note ok"><span>${num(rows.length)} rows checked: ${num(r.numbers_added)} new, ${num(r.numbers_updated)} updated, ${num(r.emails_updated)} with e-mail changes, ${num(r.unchanged)} unchanged. Press Save vendors.</span></div>`;
  } catch (e) {
    const list = e.message.replace(/^Nothing was saved\. /, "").split("; ");
    box.innerHTML = `<div class="note bad"><span><b>Nothing was saved. Fix these rows and upload again:</b></span></div><ul class="errors" style="margin-top:8px">${list.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  }
}
$("vUpload").onclick = () => { uploadRows = null; $("uFile").value = ""; $("uResult").innerHTML = ""; $("uSave").disabled = true; $("uDlg").showModal(); };
$("uFile").onchange = (e) => { if (e.target.files[0]) readUpload(e.target.files[0]); };
["dragover", "dragleave", "drop"].forEach((ev) => $("uDrop").addEventListener(ev, (e) => {
  e.preventDefault(); $("uDrop").classList.toggle("over", ev === "dragover");
  if (ev === "drop" && e.dataTransfer.files[0]) readUpload(e.dataTransfer.files[0]);
}));
$("uTemplate").onclick = () => saveCsv("vendor-master-template.csv", [["Vendor Code", "Vendor Name", "Scenario 1", "Scenario 2", "Scenario 3", "Scenario 4 Emails"], ["100001", "Sample Vendor", "9876543210", "9876543211", "9876543211", "planner@vendor.com, head@vendor.com"]]);
$("uSave").onclick = async () => {
  $("uSave").disabled = true;
  try {
    const r = await send("/vendor-numbers/bulk", { rows: uploadRows, apply: true });
    $("uDlg").close(); toast(`Saved: ${num(r.numbers_added)} new, ${num(r.numbers_updated)} updated`); loadVendors();
  } catch (e) { $("uResult").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
};

// ---------- 2 Vendor Control ----------
function initPicker() {
  C.ready = true; C.scope = ctl.scope === "all" ? "all" : "selected";
  C.picks = new Set(vendors.vendors.filter((v) => v.selected).map((v) => v.code));
}
async function loadControl() {
  try { ctl = await api("/control"); } catch (e) { $("rState").innerHTML = `<i></i><div><b>Could not load</b><span>${esc(e.message)}</span></div>`; return; }
  if (!C.ready && vendors) initPicker();
  renderControl(); renderPicker();
}
function setSeg(attr, value) { document.querySelectorAll(`[data-${attr}]`).forEach((b) => b.setAttribute("aria-pressed", b.dataset[attr] === value)); }
function renderControl() {
  const c = ctl;
  $("rState").className = "state" + (c.running ? " on" : "");
  const when = c.schedule_type === "daily" ? `every day at ${esc(c.daily_time)}` : `every ${everyText(c.every_minutes)}`;
  $("rState").innerHTML = `<i></i><div><b>${c.emergency_stop ? "Emergency stop" : c.running ? "Running" : "Stopped"}${c.busy ? " · run in progress" : ""}</b><span>${c.emergency_stop ? "Clear the emergency stop before starting again." : c.running
    ? `${c.mode === "live" ? "Live" : "Trial"}, ${when}${c.next_run ? `. Next ${inFuture(c.next_run)}` : ""}` : "Nothing runs until you press Start."}</span></div>`;
  if (!C.formReady) {
    C.formReady = true; C.mode = c.mode === "live" ? "live" : "dry"; C.sched = c.schedule_type === "daily" ? "daily" : "interval";
    const m = c.every_minutes || c.default_every_minutes, u = m % 1440 === 0 ? 1440 : m % 60 === 0 ? 60 : 1;
    $("rN").value = m / u; $("rUnit").value = String(u); $("rTime").value = c.daily_time;
  }
  setSeg("mode", C.mode); setSeg("sched", C.sched);
  $("rEvery").hidden = C.sched !== "interval"; $("rDaily").hidden = C.sched !== "daily";
  $("rModeHint").textContent = C.mode === "dry" ? "Trial: nothing is sent and vendors do not move to the next scenario." : "Live: real WhatsApp messages, calls and meeting invites.";
  $("rStart").textContent = c.running ? "Save changes" : "Start follow-up";
  $("rStop").disabled = !c.running; $("rNow").disabled = $("rRestart").disabled = c.busy || c.emergency_stop; $("rStart").disabled = c.emergency_stop;
  const runText = (r) => { if (r.error) return r.error.slice(0, 90); const s = r.summary; return s ? `${s.stopped ? "Emergency stop. " : ""}${num(s.vendor_groups)} vendor${s.vendor_groups === 1 ? "" : "s"} alerted, ${num(s.messages)} message${s.messages === 1 ? "" : "s"}` : ""; };
  $("rRuns").innerHTML = c.runs.length ? c.runs.slice(0, 5).map((r) => `<tr><td class="nowrap">${fmtDateTime(r.started_at)}<div class="sub">${r.mode === "live" ? "Live" : "Trial"}</div></td>
      <td><span class="pill ${r.status === "ok" ? "ok" : r.status === "failed" ? "bad" : r.status === "stopped" ? "warn" : "info"}">${r.status === "ok" ? "Done" : r.status === "failed" ? "Failed" : r.status === "stopped" ? "Stopped" : "Running"}</span><div class="sub">${esc(runText(r))}</div></td></tr>`).join("")
    : `<tr><td class="empty">No runs yet.</td></tr>`;
}
function renderPicker() {
  setSeg("scope", C.scope);
  const all = C.scope === "all";
  $("cPick").hidden = all; $("cAll").hidden = !all;
  if (!vendors) { $("cList").innerHTML = `<div class="empty">Loading vendors…</div>`; return; }
  const overdue = vendors.vendors.filter((v) => v.overdue_lines > 0).length;
  if (all) {
    $("cAll").innerHTML = `<div class="note"><span>Every vendor with overdue lines is followed up, ${num(overdue)} right now. New overdue vendors are included automatically.</span></div>`;
    $("cCount").textContent = "All vendors"; return;
  }
  const q = C.q.trim().toLowerCase();
  const rows = vendors.vendors.filter((v) => (!C.overdue || v.overdue_lines > 0 || C.picks.has(v.code)) && (!q || (v.name + " " + v.code).toLowerCase().includes(q)));
  rows.sort((a, b) => (C.picks.has(b.code) - C.picks.has(a.code)) || b.overdue_lines - a.overdue_lines || a.name.localeCompare(b.name));
  const shown = rows.slice(0, 200);
  $("cList").innerHTML = shown.length ? shown.map((v) => {
    const s = vendorStatus(v), on = C.picks.has(v.code);
    return `<label class="vrow ${on ? "on" : ""}"><input type="checkbox" data-pick="${esc(v.code)}" ${on ? "checked" : ""}>
      <span style="min-width:0"><span class="name">${esc(v.name)}</span><small>${esc(v.code)} · ${num(v.overdue_lines)} overdue lines</small></span>
      <span class="pill ${s.cls}">${s.label}</span></label>`;
  }).join("") + (rows.length > shown.length ? `<div class="empty small">Showing ${num(shown.length)} of ${num(rows.length)}. Search to find the others.</div>` : "")
    : `<div class="empty"><b>No vendors here</b>Try another search, or untick “With overdue lines only”.</div>`;
  const bad = vendors.vendors.filter((v) => C.picks.has(v.code) && vendorStatus(v).cls !== "ok").length;
  $("cCount").innerHTML = `<b>${num(C.picks.size)}</b> vendor${C.picks.size === 1 ? "" : "s"} selected${bad ? ` · <span style="color:var(--warn)">${num(bad)} need details in Vendor Master</span>` : ""}`;
}
$("cList").onchange = (e) => {
  if (!e.target.matches("[data-pick]")) return;
  C.dirty = true; e.target.checked ? C.picks.add(e.target.dataset.pick) : C.picks.delete(e.target.dataset.pick); renderPicker();
};
$("cQ").oninput = (e) => { C.q = e.target.value; renderPicker(); };
$("cOverdue").onchange = (e) => { C.overdue = e.target.checked; renderPicker(); };
$("cTick").onclick = () => { $("cList").querySelectorAll("[data-pick]").forEach((b) => C.picks.add(b.dataset.pick)); C.dirty = true; renderPicker(); };
$("cClear").onclick = () => { C.picks.clear(); C.dirty = true; renderPicker(); };
document.querySelector("#tab-control").addEventListener("click", (e) => {
  const s = e.target.closest("[data-scope]"), m = e.target.closest("[data-mode]"), d = e.target.closest("[data-sched]");
  if (s) { C.scope = s.dataset.scope; C.dirty = true; renderPicker(); }
  if (m) { C.mode = m.dataset.mode; if (ctl) renderControl(); }
  if (d) { C.sched = d.dataset.sched; if (ctl) renderControl(); }
});
const rFail = (m) => { $("rErr").textContent = m; $("rErr").hidden = false; };
const chosenCodes = () => (C.scope === "selected" ? [...C.picks] : []);
const noVendors = () => C.scope === "selected" && !C.picks.size;
const who = () => (C.scope === "all" ? "all vendors" : `${num(C.picks.size)} vendor${C.picks.size === 1 ? "" : "s"}`);
const needsLiveConfirm = () => C.mode === "live" && !(ctl.running && ctl.mode === "live" && C.scope === ctl.scope);
const formMinutes = () => Math.round(Number($("rN").value) * Number($("rUnit").value));
const badRepeat = () => (C.sched === "interval" && !(formMinutes() >= 1 && formMinutes() <= ctl.max_every_minutes) ? `Repeat must be between 1 minute and ${Math.round(ctl.max_every_minutes / 1440)} days.` : "");
const startBody = (live) => ({ schedule_type: C.sched, every_minutes: C.sched === "interval" ? formMinutes() : null, daily_time: $("rTime").value || null, mode: C.mode,
  confirm: live ? "LIVE" : null, scope: C.scope, codes: chosenCodes(), confirm_scope: C.scope === "all" ? "ALL" : null });
$("rStart").onclick = async () => {
  $("rErr").hidden = true;
  if (noVendors()) return rFail("Tick at least one vendor, or choose All vendors.");
  if (badRepeat()) return rFail(badRepeat());
  const live = needsLiveConfirm();
  if (live && !(await ask("Start live follow-up?", `Real WhatsApp messages, calls and meeting invites will go out for ${who()}.`, "Yes, start live"))) return;
  $("rStart").disabled = true;
  try {
    ctl = { ...ctl, ...(await send("/control/start", startBody(live))) };
    C.dirty = false; renderControl(); toast(`Follow-up saved for ${who()}`); loadVendors(); setTimeout(loadControl, 3000);
  } catch (e) { rFail(e.message); } finally { $("rStart").disabled = stopped; }
};
$("rRestart").onclick = async () => {
  $("rErr").hidden = true;
  if (noVendors()) return rFail("Tick at least one vendor, or choose All vendors.");
  if (badRepeat()) return rFail(badRepeat());
  const live = C.mode === "live";
  if (!(await ask("Restart from Scenario 1?", `Every vendor's progress is wiped and starts again from Scenario 1, whatever scenario it reached. The follow-up then runs at once for ${who()}${live ? ": real WhatsApp messages go out now" : " as a trial, so nothing is sent"}. Past messages and calls stay in the history.`, "Yes, restart"))) return;
  $("rRestart").disabled = true;
  try {
    const c = await send("/control/restart", { ...startBody(live), confirm_restart: "RESTART" });
    ctl = { ...ctl, ...c }; C.dirty = false; T.list = null; renderControl(); toast(`Restarted: ${num(c.lines_reset)} lines back to Scenario 1`); loadVendors(); setTimeout(loadControl, 1500);
  } catch (e) { rFail(e.message); loadControl(); }
};
$("rStop").onclick = async () => {
  if (!(await ask("Stop the follow-up?", "No more automatic runs will start. A run already going will finish.", "Stop"))) return;
  try { ctl = { ...ctl, ...(await send("/control/stop")) }; renderControl(); toast("Stopped"); } catch (e) { rFail(e.message); }
};
$("rNow").onclick = async () => {
  $("rErr").hidden = true;
  if (noVendors()) return rFail("Tick at least one vendor, or choose All vendors.");
  if (C.mode === "live" && !(await ask("Run live now?", `Real messages and calls will go out for ${who()}.`, "Yes, run now"))) return;
  $("rNow").disabled = true;
  try {
    await send("/vendor-selection", { scope: C.scope, codes: chosenCodes(), confirm: C.scope === "all" ? "ALL" : null }, "PUT");
    await send("/control/run-now", { mode: C.mode, confirm: C.mode === "live" ? "LIVE" : null });
    C.dirty = false; toast("Run started"); setTimeout(loadControl, 1500);
  } catch (e) { rFail(e.message); $("rNow").disabled = false; }
};

// ---------- 3 Trace ----------
function steps(level, failed) {
  const cell = (i) => {
    const cls = level != null && i <= level ? "done" : failed && i === (level == null ? 0 : level + 1) ? "bad" : "";
    return `<i class="${cls}" title="${SCENARIOS[i]}">${i + 1}</i>`;
  };
  return `<span class="steps" role="img" aria-label="${level == null ? "Not started" : SCENARIOS[level] + " reached"}">${[0, 1, 2, 3].map((i) => (i ? `<u class="${level != null && i <= level ? "done" : ""}"></u>` : "") + cell(i)).join("")}</span>`;
}
async function loadTrace() {
  try { T.list = await api("/trace"); if (!ctl) ctl = await api("/control"); } catch (e) {
    $("tBody").innerHTML = `<tr><td colspan="8" class="empty"><b>Could not load</b>${esc(e.message)}</td></tr>`; return;
  }
  renderTrace();
}
// One vendor can supply several purchasing groups: with a group chosen, the numbers are that group's lines only
const tLines = (v) => (T.pg ? v.by_pg[T.pg]?.lines ?? 0 : v.pending_lines);
const tDays = (v) => (T.pg ? v.by_pg[T.pg]?.max_od ?? 0 : v.max_od);
const tPos = (v) => (T.pg ? v.by_pg[T.pg]?.pos ?? 0 : v.po_count);
const callKey = (v) => v.call?.status || "none";
function tFiltered() {
  const q = T.q.trim().toLowerCase();
  const [lo, hi] = T.age ? T.age.split("-").map((n) => (n ? Number(n) : Infinity)) : [];
  return T.list.filter((v) => (!T.pg || T.pg in v.by_pg) && (!T.buyer || v.buyers.includes(T.buyer))
    && (!T.age || (tDays(v) >= lo && tDays(v) <= hi)) && (!T.call || callKey(v) === T.call)
    && (!T.delivery || v.problem) && (!q || (v.name + " " + v.code).toLowerCase().includes(q)));
}
function traceOptions(id, label, values, key) {
  const known = [...new Set(values)].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  if (!known.includes(T[key])) T[key] = "";
  $(id).innerHTML = `<option value="">${label}</option>` + known.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  $(id).value = T[key];
}
function renderTrace() {
  const all = T.list;
  traceOptions("tPg", "All purchasing groups", all.flatMap((v) => Object.keys(v.by_pg)), "pg");
  traceOptions("tBuyer", "All buyers", all.flatMap((v) => v.buyers), "buyer");
  const filtered = tFiltered();
  $("tSub").textContent = `Only the vendors you chose in Vendor Control${ctl?.synced_at ? ` · PO Master updated ${fmtDateTime(ctl.synced_at)}` : ""}`;
  const by = (lv) => filtered.filter((v) => (lv === "none" ? v.level == null : v.level === lv)).length;
  const tiles = [["any", "Your vendors", filtered.length], ["none", "Not started", by("none")], [0, "Scenario 1", by(0)], [1, "Scenario 2", by(1)], [2, "Scenario 3", by(2)], [3, "Scenario 4", by(3)]];
  $("tTiles").innerHTML = tiles.map(([k, l, c]) => `<button class="tile" data-tl="${k}" aria-pressed="${String(T.level) === String(k)}"><span>${l}</span><b>${num(c)}</b></button>`).join("");
  const rows = filtered.filter((v) => T.level === "any" || (T.level === "none" ? v.level == null : v.level === T.level))
    .sort((a, b) => ((b.level ?? -1) - (a.level ?? -1)) || tLines(b) - tLines(a));
  T.rows = rows;
  const shown = rows.slice(0, T.limit);
  $("tClear").hidden = !(T.q || T.pg || T.buyer || T.age || T.call || T.delivery || T.level !== "any");
  const groups = (v) => { const g = Object.keys(v.by_pg); return g.length > 3 ? `${g.slice(0, 3).join(", ")} +${g.length - 3}` : g.join(", "); };
  const cycle = (v) => (v.restart_on ? `Back to Scenario 1 on ${fmtDay(v.restart_on)}` : v.cycle_day ? `Day ${v.cycle_day} of this cycle` : "");
  const lastCall = (v) => (v.call ? `${callPill(v.call)}<div class="sub">${[ago(v.call.placed_at), v.call.analysis?.earliest_commitment && "promised " + fmtDay(v.call.analysis.earliest_commitment), topReason(v.call.analysis)].filter(Boolean).map(esc).join(" · ")}</div>` : `<span class="muted">Not called</span>`);
  $("tBody").innerHTML = shown.length ? shown.map((v) => `<tr class="click" tabindex="0" data-trace="${esc(v.code)}"><td><div class="name">${esc(v.name)}</div><div class="sub">${esc(v.code)}</div></td>
      <td title="${esc(Object.keys(v.by_pg).join(", "))}">${esc(groups(v)) || `<span class="muted">—</span>`}${v.buyers.length ? `<div class="sub">${esc(v.buyers.join(", "))}</div>` : ""}</td>
      <td class="r">${num(tLines(v))}</td><td class="r">${num(tPos(v))}</td><td class="r">${tDays(v) ? num(tDays(v)) : "—"}</td><td>${steps(v.level, v.problem)}${cycle(v) ? `<div class="sub">${esc(cycle(v))}</div>` : ""}${v.problem ? `<div class="sub" style="color:var(--bad)">Not delivered: ${esc(v.problem)}</div>` : ""}</td>
      <td>${lastCall(v)}</td><td class="nowrap">${v.last_alert_at ? ago(v.last_alert_at) : `<span class="muted">Nothing sent yet</span>`}</td></tr>`).join("")
    : all.length ? `<tr><td colspan="8" class="empty"><b>No vendors here</b>Try another filter.</td></tr>`
      : `<tr><td colspan="8" class="empty"><b>No vendors chosen yet</b>Pick the vendors to follow up, then come back here.<br><button class="primary" onclick="showTab('control')">Go to Vendor Control</button></td></tr>`;
  $("tRange").textContent = rows.length ? `${num(shown.length)} of ${num(rows.length)} vendors` : "";
  $("tMore").hidden = rows.length <= shown.length;
}
$("tTiles").onclick = (e) => { const b = e.target.closest("[data-tl]"); if (b) { const k = b.dataset.tl; T.level = /^\d$/.test(k) ? Number(k) : k; T.limit = 50; renderTrace(); } };
$("tQ").oninput = (e) => { T.q = e.target.value; if (T.list) renderTrace(); };
$("tMore").onclick = () => { T.limit += 100; renderTrace(); };
for (const [id, key] of [["tPg", "pg"], ["tBuyer", "buyer"], ["tAge", "age"], ["tCall", "call"], ["tDelivery", "delivery"]]) {
  $(id).onchange = (e) => { T[key] = e.target.value; T.limit = 50; if (T.list) renderTrace(); };
}
$("tClear").onclick = () => {
  Object.assign(T, { q: "", level: "any", limit: 50, pg: "", buyer: "", age: "", call: "", delivery: "" });
  for (const id of ["tQ", "tAge", "tCall", "tDelivery"]) $(id).value = "";
  renderTrace();
};
$("tCsv").onclick = () => {
  if (!T.rows) return;
  saveCsv("trace.csv", [["Vendor code", "Vendor", "Purchasing groups", "Buyers", "Overdue lines", "POs", "Max days late", "Scenario", "Cycle day", "Restarts on", "Last call", "Call time", "Promised date", "Delay reason", "Last action", "Not delivered"],
    ...T.rows.map((v) => [v.code, v.name, Object.keys(v.by_pg).join(" "), v.buyers.join(" "), tLines(v), tPos(v), tDays(v), v.level == null ? "Not started" : SCENARIOS[v.level], v.cycle_day ?? "", v.restart_on ?? "",
      v.call?.status?.replace(/_/g, " ") ?? "Not called", v.call ? fmtDateTime(v.call.placed_at) : "", v.call?.analysis?.earliest_commitment ?? "", topReason(v.call?.analysis), v.last_alert_at ? fmtDateTime(v.last_alert_at) : "", v.problem])]);
};
$("tBody").onclick = (e) => { const r = e.target.closest("[data-trace]"); if (r) openTrace(r.dataset.trace); };
$("tBody").onkeydown = (e) => { const r = e.target.closest("[data-trace]"); if (r && e.key === "Enter") openTrace(r.dataset.trace); };

async function openTrace(code) {
  openDrawer(code, "Loading…", `<p class="muted">Loading the trace…</p>`);
  let t, calls = [];
  try { [t, calls] = await Promise.all([api("/trace/" + encodeURIComponent(code)), api("/calls?limit=20&vendor=" + encodeURIComponent(code)).catch(() => [])]); }
  catch (e) { $("dSub").textContent = ""; $("dBody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  const c = t.contacts || {};
  const stepItem = (s) => {
    const failed = s.state !== "done" && s.messages.some((m) => m.error);
    const when = s.state === "done" ? (s.done_at ? "Done " + fmtDateTime(s.done_at) : "Done") : s.state === "next" ? (s.due_now ? "Due at the next run" : "Due " + fmtDateTime(s.due_at)) : "Later";
    const msgs = s.messages.slice(0, 3).map((m) => `<li class="${m.error ? "fail" : ""}">${esc(fmtDateTime(m.at))} · ${esc(m.name || m.role || "")} · ${esc(m.error || String(m.status).replace(/_/g, " ").toLowerCase())}</li>`).join("");
    const callBtns = s.level === 2 && calls.length ? `<div class="actions" style="margin-top:8px">${calls.slice(0, 3).map((x) => `<button class="sm" data-call="${esc(x.key)}">Call ${esc(fmtDateTime(x.placed_at))}${x.analysis?.earliest_commitment ? " · promised " + esc(fmtDay(x.analysis.earliest_commitment)) : ""}</button>`).join("")}</div>` : "";
    return `<li class="${failed ? "failed" : s.state === "done" ? "done" : s.state === "next" ? "next" : ""}"><span class="dot">${s.level + 1}</span><div>
      <div class="t1">${esc(SCENARIOS[s.level])} · ${esc(STEP_TEXT[s.level][0])}</div><div class="t2">${esc(STEP_TEXT[s.level][1])} · ${esc(when)}</div>${msgs ? `<ul>${msgs}</ul>` : ""}${callBtns}</div></li>`;
  };
  const lines = t.lines.filter((l) => !/CLEARED/i.test(l.status) && (!T.pg || l.pg === T.pg)).sort((a, b) => (Number(b.od) || 0) - (Number(a.od) || 0));
  const pgs = Object.entries(t.by_pg).map(([g, x]) => `${g} (${num(x.lines)})`).join(", ");
  openDrawer(t.name, `Vendor ${code} · ${num(t.pending_lines)} overdue lines · ${num(t.po_count)} POs`, `
    ${t.next_note ? `<div class="note"><span>${esc(t.next_note)}</span></div>` : ""}
    <section><h3>This cycle</h3><dl class="facts"><div><dt>Cycle</dt><dd>${t.cycle_day ? `No. ${num(t.cycle_number)}, day ${t.cycle_day}` : "Not started"}</dd></div><div><dt>Started</dt><dd>${t.cycle_start ? esc(fmtDay(t.cycle_start)) : "—"}</dd></div><div><dt>Back to Scenario 1</dt><dd>${t.restart_on ? esc(fmtDay(t.restart_on)) : `${t.cycle_days} days after Scenario 4`}</dd></div><div class="wide"><dt>Purchasing groups (overdue lines)</dt><dd>${esc(pgs) || "—"}</dd></div></dl></section>
    <section><ol class="timeline">${t.steps.map(stepItem).join("")}</ol></section>
    <section><h3>Contacts used</h3><dl class="facts"><div><dt>Scenario 1</dt><dd>${esc(c.scenario1 || "—")}</dd></div><div><dt>Scenario 2</dt><dd>${esc(c.scenario2 || "—")}</dd></div><div><dt>Scenario 3 call</dt><dd>${esc(c.scenario3 || "—")}</dd></div><div><dt>Buyer</dt><dd>${esc(c.buyer_email || c.buyer_mobile || "—")}</dd></div></dl></section>
    ${lines.length ? `<section><h3>Overdue lines${T.pg ? ` · purchasing group ${esc(T.pg)}` : ""}</h3><div class="table-wrap"><table class="t"><thead><tr><th>PO</th><th>Material</th><th>Group</th><th class="r">Pending</th><th class="r">Days late</th><th>Scenario</th></tr></thead><tbody>${lines.slice(0, 30).map((l) => `<tr><td class="mono">${esc(l.po)}</td><td>${esc(l.desc)}</td><td>${esc(l.pg)}</td><td class="r nowrap">${esc(l.pending)}</td><td class="r">${esc(l.od)}</td><td class="nowrap">${l.level == null ? `<span class="muted">Not started</span>` : esc(SCENARIOS[l.level])}</td></tr>`).join("")}</tbody></table></div>${lines.length > 30 ? `<p class="hint">First 30 of ${num(lines.length)} lines, latest first.</p>` : ""}</section>` : ""}`);
}
$("dBody").addEventListener("click", (e) => { const b = e.target.closest("[data-call]"); if (b) openCall(b.dataset.call); });

// ---------- 4 Call Results ----------
const callPill = (c) => {
  const [cls, label] = { completed: ["ok", "Answered"], no_response: ["warn", "No response"], failed: ["bad", "Failed"], in_call: ["info", "In call"], placed: ["info", "Ringing"] }[c.status] || ["", c.status];
  return `<span class="pill ${cls}">${label}</span>`;
};
const topReason = (a) => {
  const c = {};
  for (const l of a?.lines || []) if (l.reason_category && l.reason_category !== "none") c[l.reason_category] = (c[l.reason_category] || 0) + 1;
  const k = Object.keys(c).sort((x, y) => c[y] - c[x])[0];
  return k ? REASON[k] : "";
};
async function loadCalls() {
  try { [L.list, L.insights] = await Promise.all([api("/calls?limit=300"), api("/calls/insights?days=30")]); }
  catch (e) { $("lBody").innerHTML = `<tr><td colspan="6" class="empty"><b>Could not load calls</b>${esc(e.message)}</td></tr>`; return; }
  const i = L.insights;
  $("lTiles").innerHTML = [["Calls in 30 days", i.calls], ["Answered", i.answered], ["Dates committed", i.lines_with_date], ["Need checking", i.needs_review]].map(([l, v]) => `<div class="tile"><span>${l}</span><b>${num(v)}</b></div>`).join("");
  const max = Math.max(1, ...i.reasons.map((r) => r.lines));
  $("lReasonsCard").hidden = !i.reasons.length;
  $("lReasons").innerHTML = i.reasons.map((r) => `<div class="bar"><span>${esc(REASON[r.reason] || r.reason)}</span><div class="track"><div class="fill" style="width:${(100 * r.lines) / max}%"></div></div><span class="v">${num(r.lines)}</span></div>`).join("");
  $("lBody").innerHTML = L.list.length ? L.list.map((c) => {
    const a = c.analysis;
    const date = a?.earliest_commitment ? `<b>${esc(fmtDay(a.earliest_commitment))}</b>` : c.status === "completed" ? `<span class="muted">${c.analysis_status === "pending" ? "Reading call…" : c.analysis_status === "failed" ? "Could not read" : "No firm date"}</span>` : `<span class="muted">—</span>`;
    return `<tr class="click" tabindex="0" data-call="${esc(c.key)}"><td class="nowrap">${fmtDateTime(c.placed_at)}</td><td><div class="name">${esc(c.vendor_name)}</div><div class="sub">${esc(c.vendor_code)}</div></td>
      <td>${callPill(c)}</td><td>${esc(a?.contact_name) || `<span class="muted">—</span>`}</td><td>${date}</td><td>${esc(topReason(a)) || `<span class="muted">—</span>`}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty"><b>No calls yet</b>Scenario 3 calls appear here with the transcript, delay reason and committed date.</td></tr>`;
}
$("lBody").onclick = (e) => { const r = e.target.closest("[data-call]"); if (r) openCall(r.dataset.call); };
$("lBody").onkeydown = (e) => { const r = e.target.closest("[data-call]"); if (r && e.key === "Enter") openCall(r.dataset.call); };
async function openCall(key) {
  openDrawer("Call", "Loading…", `<p class="muted">Loading the call…</p>`);
  try { renderCall(await api("/calls/" + encodeURIComponent(key))); }
  catch (e) { $("dSub").textContent = ""; $("dBody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}
function renderCall(c) {
  const a = c.analysis;
  const lines = a?.lines?.length ? `<section><h3>What the vendor said</h3><div class="table-wrap"><table class="t"><thead><tr><th>PO</th><th>Status</th><th>Committed</th><th>Delay reason</th></tr></thead><tbody>${a.lines.map((l) => `<tr><td class="mono">${esc(l.po)}</td><td>${esc(LINE_STATUS[l.status] || l.status)}</td><td class="nowrap">${l.committed_date ? `<b>${esc(fmtDay(l.committed_date))}</b>` : "—"}</td><td>${esc(l.delay_reason) || "—"}</td></tr>`).join("")}</tbody></table></div></section>` : "";
  openDrawer(c.vendor_name, `${fmtDateTime(c.placed_at)} · ${c.mobile}`, `
    ${c.analysis_status === "failed" ? `<div class="note bad"><span>The transcript was saved but could not be read: ${esc(c.analysis_error || "")}</span></div>` : ""}
    ${a?.summary ? `<section><h3>Summary</h3><p>${esc(a.summary)}</p></section>` : ""}
    ${a ? `<dl class="facts"><div><dt>Spoke to</dt><dd>${esc(a.contact_name || "—")}</dd></div><div><dt>Earliest commitment</dt><dd>${a.earliest_commitment ? esc(fmtDay(a.earliest_commitment)) : "None given"}</dd></div><div class="wide"><dt>Support asked for</dt><dd>${esc(a.support_needed || "None")}</dd></div></dl>` : ""}
    ${a?.flags?.length ? `<div class="note warn"><span>${a.flags.map(esc).join("<br>")}</span></div>` : ""}${lines}
    <section><h3>Transcript</h3>${c.transcript?.length ? `<div class="chat">${c.transcript.map((t) => `<div class="msg ${t.role === "agent" ? "agent" : ""}"><small>${t.role === "agent" ? "Agent" : "Vendor"}</small>${esc(t.text)}</div>`).join("")}</div>` : `<p class="muted">${c.status === "failed" ? "The call was not placed: " + esc(c.analysis_error || "") : c.status === "placed" || c.status === "in_call" ? "The call is not finished yet." : "Nobody spoke on this call."}</p>`}</section>
    <div class="actions">${c.recording_url ? `<a class="btn" href="${esc(c.recording_url)}" target="_blank" rel="noopener noreferrer">Play recording</a>` : ""}${c.transcript?.length ? `<button id="reRead">Read transcript again</button>` : ""}</div>`);
  const b = $("reRead");
  if (b) b.onclick = async () => {
    b.disabled = true;
    try { renderCall(await send(`/calls/${encodeURIComponent(c.key)}/analyse`)); loadCalls(); } catch (e) { toast(e.message, true); b.disabled = false; }
  };
}
$("lCsv").onclick = () => {
  if (!L.list) return;
  const rows = [];
  for (const c of L.list) for (const l of c.analysis?.lines || []) rows.push([fmtDateTime(c.placed_at), c.vendor_code, c.vendor_name, c.analysis.contact_name, l.po, l.item, LINE_STATUS[l.status] || l.status, l.committed_date, l.committed_quantity, REASON[l.reason_category] || "", l.delay_reason]);
  saveCsv("call-results.csv", [["Call time", "Vendor code", "Vendor", "Spoke to", "PO", "Item", "Status", "Committed date", "Quantity", "Delay category", "Delay reason"], ...rows]);
};

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
if (session) showTab(location.hash.slice(1) || "vendors");
