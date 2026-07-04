/* Kiroshi advisory notifier — shared across every dashboard page.
 *
 * The Coordinator already exposes /advisories with structured NAS-contention
 * warnings (nas.thrash, nas.disk_saturation, nas.throughput_collapse,
 * nas.parity_write_pressure, sub-job.failure_spike). This script polls that
 * endpoint and, for every advisory whose (fingerprint, count) pair we
 * have not shown yet, raises:
 *   - a native Windows notification (Notification API) — works even when
 *     the browser tab is not focused, which is the whole point;
 *   - and, if the tab IS focused, an in-page toast, so we never *only*
 *     rely on OS-level toasts (users disable those; a visible banner is
 *     harder to miss when they're looking at the dashboard).
 *
 * Dedup key is `${fingerprint}#${count}`: same condition still firing bumps
 * `count` server-side, so a persistent thrash surfaces as one initial pop
 * plus one refresher every N ticks (advisory count grows continuously — we
 * throttle repeat pops to at most one per 5 min per fingerprint).
 *
 * Snooze/dismiss state lives in localStorage so refreshing the page does
 * not re-fire an alert you already saw.
 */
(function(){
  if (window.__kiroshiAdvisoryNotifier) return; // idempotent
  window.__kiroshiAdvisoryNotifier = true;

  const POLL_MS = 5000;
  const REPOP_MIN_MS = 5 * 60 * 1000; // don't re-notify same fingerprint more often than this
  const SNOOZE_MS = 15 * 60 * 1000;    // "Snooze" button suppresses that fingerprint for 15 min

  function tok(){ try { return localStorage.getItem("kiroshi_token") || ""; } catch(e){ return ""; } }
  async function aFetch(url){
    const o = { cache: "no-store", headers: {} };
    const t = tok(); if (t) o.headers.Authorization = "Bearer " + t;
    return fetch(url, o);
  }

  function loadState(){
    try { return JSON.parse(localStorage.getItem("kiroshi_adv_state") || "{}"); }
    catch(e) { return {}; }
  }
  function saveState(s){
    try { localStorage.setItem("kiroshi_adv_state", JSON.stringify(s)); } catch(e){}
  }
  const state = loadState(); // { [fingerprint]: { lastCount, lastShownAt, snoozedUntil } }

  async function ensureNotifPerm(){
    if (!("Notification" in window)) return false;
    if (Notification.permission === "granted") return true;
    if (Notification.permission === "denied") return false;
    try { const p = await Notification.requestPermission(); return p === "granted"; }
    catch(e){ return false; }
  }

  // ---------- in-page toast ----------
  function ensureToastContainer(){
    let c = document.getElementById("kiroshi-adv-toasts");
    if (c) return c;
    c = document.createElement("div");
    c.id = "kiroshi-adv-toasts";
    c.style.cssText = "position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:10px;z-index:9999;max-width:420px;font-family:'Segoe UI',system-ui,sans-serif;";
    document.body.appendChild(c);
    return c;
  }
  function severityColor(sev){
    if (sev === "critical") return "#ff2e88";
    if (sev === "warn")     return "#ffb648";
    return "#1ad1c8";
  }
  function esc(s){ return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

  function showToast(adv){
    const c = ensureToastContainer();
    const color = severityColor(adv.severity);
    const div = document.createElement("div");
    div.style.cssText = `background:#0d1117;border:1px solid ${color};border-left:4px solid ${color};border-radius:8px;padding:12px 14px;color:#d7e0ea;box-shadow:0 0 24px rgba(0,0,0,.5), 0 0 12px ${color}33;font-size:13px;line-height:1.4;`;
    const diskFrag = adv.disk ? ` <span style="color:#6b7a8d">disk=${esc(adv.disk)}</span>` : "";
    div.innerHTML =
      `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
         <span style="color:${color};font-weight:700;letter-spacing:.2em;text-transform:uppercase;font-size:11px;">⚠ ${esc(adv.severity)}</span>
         <span style="color:${color};font-weight:700;">${esc(adv.code)}</span>${diskFrag}
         <button type="button" style="margin-left:auto;background:transparent;border:none;color:#6b7a8d;font-size:16px;cursor:pointer;line-height:1;">×</button>
       </div>
       <div style="margin-bottom:6px;">${esc(adv.detail)}</div>
       <div style="color:#6b7a8d;font-size:12px;margin-bottom:8px;">action: ${esc(adv.suggested_action)}</div>
       <div style="display:flex;gap:8px;">
         <button type="button" data-act="snooze" style="cursor:pointer;background:transparent;color:#ffb648;border:1px solid #1b2230;border-radius:5px;padding:3px 9px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;">Snooze 15m</button>
         <button type="button" data-act="dismiss" style="cursor:pointer;background:transparent;color:#6b7a8d;border:1px solid #1b2230;border-radius:5px;padding:3px 9px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;">Dismiss</button>
       </div>`;
    const close = () => { if (div.parentNode) div.parentNode.removeChild(div); };
    div.querySelector('button[style*="margin-left:auto"]').onclick = close;
    div.querySelector('button[data-act="dismiss"]').onclick = close;
    div.querySelector('button[data-act="snooze"]').onclick = () => {
      state[adv.fingerprint] = Object.assign({}, state[adv.fingerprint], {
        snoozedUntil: Date.now() + SNOOZE_MS,
      });
      saveState(state);
      close();
    };
    c.appendChild(div);
    // Auto-fade critical toasts stay until clicked; warns fade after 60s.
    if (adv.severity !== "critical") setTimeout(close, 60000);
  }

  async function fireNative(adv){
    const ok = await ensureNotifPerm();
    if (!ok) return;
    try {
      const title = `KIROSHI: ${adv.severity.toUpperCase()} · ${adv.code}` + (adv.disk ? ` (${adv.disk})` : "");
      const n = new Notification(title, {
        body: adv.detail + "\naction: " + adv.suggested_action,
        tag: "kiroshi-adv-" + adv.fingerprint,
        requireInteraction: adv.severity === "critical",
      });
      n.onclick = () => {
        window.focus();
        if (adv.dashboard_url) {
          try { window.open(adv.dashboard_url, "_blank"); } catch(e){}
        }
        n.close();
      };
    } catch(e){ /* ignore */ }
  }

  function shouldFire(adv){
    const now = Date.now();
    const s = state[adv.fingerprint] || {};
    if (s.snoozedUntil && now < s.snoozedUntil) return false;
    // Fire when either it's brand new, or its count grew AND it's been long
    // enough since our last pop.
    if (s.lastCount == null) return true;
    if (adv.count > s.lastCount && now - (s.lastShownAt || 0) >= REPOP_MIN_MS) return true;
    return false;
  }

  async function poll(){
    try {
      const r = await aFetch("/advisories?active_only=true&limit=50");
      if (!r.ok) return;
      const body = await r.json();
      const advs = body.advisories || [];
      for (const adv of advs){
        if (!shouldFire(adv)) continue;
        state[adv.fingerprint] = {
          lastCount: adv.count,
          lastShownAt: Date.now(),
          snoozedUntil: (state[adv.fingerprint]||{}).snoozedUntil || 0,
        };
        saveState(state);
        // Native OS toast whenever the tab is not focused; in-page toast is
        // always shown so a user staring at the dashboard also sees it.
        if (document.hidden) fireNative(adv);
        showToast(adv);
        if (document.hidden) { /* also try native even if we showed toast? no — toast will be seen when they return */ }
        else {
          // Fire native too so the taskbar flashes even for a focused tab
          // (Windows will suppress duplicate 'tag' toasts if visible).
          fireNative(adv);
        }
      }
    } catch(e){ /* network hiccups are fine — advisories are best-effort */ }
  }

  // Request permission once, on first user gesture OR quietly at load.
  ensureNotifPerm();
  document.addEventListener("click", ensureNotifPerm, { once: true });

  poll();
  setInterval(poll, POLL_MS);
})();
