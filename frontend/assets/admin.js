"use strict";
/* Admin: system tests (WhatsApp, call, meeting) and SCM users. */
const session = Session.require("admin");
const adminApi = (path, opts) => request("/admin/api" + path, opts);
const adminPost = (path, body, method) => post("/admin/api" + path, body, method);
const TABS = ["tests", "users"];

function showTab(t) {
  if (!TABS.includes(t)) t = "tests";
  for (const x of TABS) $("tab-" + x).hidden = x !== t;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t));
  history.replaceState(null, "", "#" + t);
  ({ tests: loadChecks, users: loadUsers })[t]();
}
document.querySelectorAll("[data-tab]").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

// ---------- system tests ----------
async function loadChecks() {
  const cards = [["Whatsapp", "whatsapp"], ["Call", "call"], ["Meeting", "meeting"]];
  try {
    const s = await adminApi("/checks");
    cards.forEach(([id, key]) => {
      $("s" + id).textContent = s[key].ok ? "Connected" : "Problem"; $("s" + id).className = "pill " + (s[key].ok ? "ok" : "bad");
      $("d" + id).textContent = s[key].detail; $("d" + id).style.color = s[key].ok ? "" : "var(--bad)";
    });
  } catch (e) { toast(e.message, true); }
}
function remember(id) {
  try { $(id).value = localStorage.getItem("test_" + id) || ""; } catch (e) {}
  $(id).addEventListener("change", () => { try { localStorage.setItem("test_" + id, $(id).value); } catch (e) {} });
}
["wTo", "cTo", "mTo"].forEach(remember);
function runTest(form, button, out, run, done) {
  $(form).onsubmit = async (e) => {
    e.preventDefault();
    $(button).disabled = true; $(out).className = "result muted"; $(out).textContent = "Working…";
    try { const r = await run(); $(out).className = "result note ok"; $(out).innerHTML = done(r); }
    catch (err) { $(out).className = "result note bad"; $(out).textContent = err.message; }
    finally { $(button).disabled = false; }
  };
}
runTest("wForm", "wGo", "wOut", () => adminPost("/checks/whatsapp", { mobile: $("wTo").value, message: $("wText").value }), (r) => `Sent to ${esc(r.to)}. Check the phone.`);
runTest("cForm", "cGo", "cOut", () => adminPost("/checks/call", { mobile: $("cTo").value }), (r) => `Calling ${esc(r.to)} now.`);
runTest("mForm", "mGo", "mOut", () => adminPost("/checks/meeting", { email: $("mTo").value }), (r) => `Invite sent to ${esc(r.to)} for ${esc(fmtDateTime(r.starts))}.${r.meet_link ? ` <a href="${esc(r.meet_link)}" target="_blank" rel="noopener noreferrer">Open Meet</a>` : ""}`);

// ---------- users ----------
function randomPassword() {
  const chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ", digits = "23456789", a = new Uint32Array(10);
  crypto.getRandomValues(a);
  return [...a].map((x, i) => (i % 4 === 3 ? digits[x % digits.length] : chars[x % chars.length])).join("");
}
async function loadUsers() {
  try {
    const [list, log] = await Promise.all([adminApi("/users"), adminApi("/audit?limit=40")]);
    $("uBody").innerHTML = list.length ? list.map((u) => `<tr><td class="bold">${esc(u.username)}</td><td>${esc(u.display_name)}</td>
      <td><span class="pill ${!u.active ? "" : u.locked ? "warn" : "ok"}">${!u.active ? "Off" : u.locked ? "Locked" : "Active"}</span></td><td class="nowrap">${u.last_login_at ? fmtDateTime(u.last_login_at) : `<span class="muted">Never</span>`}</td>
      <td class="r"><span class="actions" style="justify-content:flex-end"><button class="sm" data-pass="${esc(u.username)}">Reset password</button><button class="sm" data-active="${esc(u.username)}" data-to="${u.active ? 0 : 1}">${u.active ? "Switch off" : "Switch on"}</button><button class="sm danger" data-del="${esc(u.username)}">Delete</button></span></td></tr>`).join("")
      : `<tr><td colspan="5" class="empty"><b>No users yet</b>Create the first one above.</td></tr>`;
    $("aBody").innerHTML = log.length ? log.map((r) => `<tr><td class="nowrap">${fmtDateTime(r.at)}</td><td>${esc(r.actor)}</td><td>${esc(r.action.replace(/_/g, " "))}</td>
      <td class="small muted">${esc(Object.entries(r.detail || {}).filter(([, v]) => v !== null && v !== "").map(([k, v]) => `${k}: ${Array.isArray(v) ? (v.length > 6 ? v.length + " vendors" : v.join(", ")) : v}`).join(" · "))}</td></tr>`).join("")
      : `<tr><td colspan="4" class="empty">Nothing yet.</td></tr>`;
  } catch (e) { toast(e.message, true); }
}
$("uGen").onclick = () => { $("uPass").value = randomPassword(); };
$("uForm").onsubmit = async (e) => {
  e.preventDefault(); $("uGo").disabled = true; $("uDone").hidden = true;
  try {
    const pass = $("uPass").value;
    const u = await adminPost("/users", { username: $("uId").value, password: pass, display_name: $("uName").value });
    $("uDone").textContent = `Created. User id: ${u.username}  Password: ${pass}  (note it now, it is not shown again)`;
    $("uDone").hidden = false; $("uForm").reset(); loadUsers();
  } catch (err) { toast(err.message, true); } finally { $("uGo").disabled = false; }
};
$("uBody").onclick = async (e) => {
  const p = e.target.closest("[data-pass]"), a = e.target.closest("[data-active]"), d = e.target.closest("[data-del]");
  try {
    if (p) {
      const value = randomPassword();
      await adminPost("/users/" + encodeURIComponent(p.dataset.pass), { password: value }, "PATCH");
      $("uDone").textContent = `New password for ${p.dataset.pass}: ${value}  (note it now, it is not shown again)`; $("uDone").hidden = false; window.scrollTo(0, 0); loadUsers();
    } else if (a) {
      await adminPost("/users/" + encodeURIComponent(a.dataset.active), { active: a.dataset.to === "1" }, "PATCH"); loadUsers();
    } else if (d && await ask(`Delete ${d.dataset.del}?`, "This user can no longer sign in.", "Delete")) {
      await adminApi("/users/" + encodeURIComponent(d.dataset.del), { method: "DELETE" }); toast("User deleted"); loadUsers();
    }
  } catch (err) { toast(err.message, true); }
};

if (session) showTab(location.hash.slice(1) || "tests");
