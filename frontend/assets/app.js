"use strict";
/* Shared by the login, SCM user and admin pages: session, API calls, formatting, toast, drawer. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => Number(x || 0).toLocaleString("en-IN");
const APP_TZ = "Asia/Kolkata";
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { timeZone: APP_TZ, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
const fmtDay = (d) => (d ? new Date(d + "T00:00:00+05:30").toLocaleDateString("en-IN", { timeZone: APP_TZ, day: "numeric", month: "short", year: "numeric" }) : "");
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}
function inFuture(iso) {
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  if (m <= 0) return "now";
  if (m < 60) return `in ${m} min`;
  const h = Math.round(m / 60);
  return h < 48 ? `in ${h} h` : `in ${Math.round(h / 24)} days`;
}
const everyText = (m) => (m % 1440 === 0 ? `${m / 1440} day${m === 1440 ? "" : "s"}` : m % 60 === 0 ? `${m / 60} hour${m === 60 ? "" : "s"}` : `${m} minute${m === 1 ? "" : "s"}`);

const Session = {
  get() { try { return JSON.parse(sessionStorage.getItem("po_session") || "null"); } catch (e) { return null; } },
  set(s) { try { sessionStorage.setItem("po_session", JSON.stringify(s)); } catch (e) {} },
  clear() { try { sessionStorage.removeItem("po_session"); } catch (e) {} },
  // Sends the visitor to the login page unless signed in with this role
  require(role) {
    const s = this.get();
    if (!s || s.role !== role) { location.replace("/"); return null; }
    return s;
  },
  home: (role) => (role === "admin" ? "/admin" : "/dashboard"),
};

async function request(url, opts = {}) {
  const s = Session.get();
  const res = await fetch(url, { ...opts, headers: { "Content-Type": "application/json", ...(s ? { "X-Session": s.token } : {}), ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && s) { Session.clear(); location.replace("/"); throw new Error("Session ended"); }
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}
const post = (url, body, method = "POST") => request(url, { method, body: JSON.stringify(body ?? {}) });

let toastTimer;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
}
function openDrawer(title, sub, html) {
  $("dTitle").textContent = title; $("dSub").textContent = sub; $("dBody").innerHTML = html;
  $("scrim").hidden = $("drawer").hidden = false; document.body.style.overflow = "hidden";
}
function closeDrawer() { $("scrim").hidden = $("drawer").hidden = true; document.body.style.overflow = ""; }
function signOut() { Session.clear(); location.replace("/"); }
// One confirmation dialog for every page; resolves true when the user agrees
function ask(title, text, yes) {
  return new Promise((resolve) => {
    $("askTitle").textContent = title; $("askText").textContent = text; $("askYes").textContent = yes;
    const d = $("askDlg"); d.returnValue = "";
    d.onclose = () => resolve(d.returnValue === "yes");
    d.showModal();
  });
}
document.addEventListener("DOMContentLoaded", () => document.body.insertAdjacentHTML("beforeend", `
  <dialog id="askDlg" aria-labelledby="askTitle"><form method="dialog">
    <div class="dlg-h"><h2 id="askTitle"></h2></div>
    <div class="dlg-b"><p id="askText"></p></div>
    <div class="dlg-f"><button value="no">Cancel</button><button class="primary" value="yes" id="askYes"></button></div>
  </form></dialog>
  <div id="toast" role="status" aria-live="polite" hidden></div>`));
function saveCsv(name, rows) {
  const cell = (v) => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + rows.map((r) => r.map(cell).join(",")).join("\r\n")], { type: "text/csv" }));
  a.download = name; a.click(); URL.revokeObjectURL(a.href);
}
