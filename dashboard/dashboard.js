"use strict";
const byId = id => document.getElementById(id);
const modes = ["mtproto", "telegram", "web"];
const modeLabels = {mtproto: "MTProto", telegram: "Telegram Web Link", web: "HTTP / SOCKS"};
const protocolNames = {http: "HTTP · HTTPS tunnel", https: "HTTPS · TLS to proxy", socks4: "SOCKS4", socks5: "SOCKS5"};
const snapshots = new Map();
let mode = modes.includes(new URLSearchParams(location.search).get("mode")) ? new URLSearchParams(location.search).get("mode") : "mtproto";
let paused = false;
let pollTimer;
let activeRequest;
let generation = 0;
let renderedIdentity = "";
let connected = false;
const count = value => Number.isInteger(value) && value >= 0 ? value.toLocaleString() : "—";
const percentage = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";

function status(text, error = false) {
  byId("status").textContent = text;
  byId("status").classList.toggle("error", error);
}

function secretIdentity(value) {
  if (typeof value !== "string" || !value || value.length > 600) return null;
  value = value.trim();
  try {
    let bytes;
    if (/^[0-9a-f]+$/i.test(value) && value.length % 2 === 0) {
      bytes = value.match(/../g).map(part => Number.parseInt(part, 16));
    } else {
      if (!/^[A-Za-z0-9_+/-]+={0,2}$/.test(value)) return null;
      const encoded = value.replaceAll("-", "+").replaceAll("_", "/");
      bytes = Array.from(atob(encoded + "=".repeat((4 - encoded.length % 4) % 4)), character => character.charCodeAt(0));
    }
    let valid = bytes.length === 16 || bytes.length === 17 && bytes[0] === 0xdd;
    if (bytes.length >= 18 && bytes.length <= 199 && bytes[0] === 0xee) {
      const rawDomain = bytes.slice(17);
      const domain = String.fromCharCode(...rawDomain);
      const labels = domain.replace(/\.$/, "").split(".");
      const ipv4 = /^(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
      valid = rawDomain.every(byte => byte < 128) && domain.length <= 182 && !domain.endsWith("..") && labels.every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)) && (!/^[0-9.]+$/.test(domain) || ipv4.test(domain));
    }
    return valid ? bytes.map(byte => byte.toString(16).padStart(2, "0")).join("") : null;
  } catch { return null; }
}

function safeConnection(proxy) {
  const text = proxy[{mtproto: "tg_link", telegram: "web_link", web: "uri"}[mode]];
  if (typeof text !== "string" || text.length > 2048 || /[\u0000-\u0020]/.test(text)) return null;
  try {
    const parsed = new URL(text);
    const server = String(proxy.server).replace(/^\[|\]$/g, "").toLowerCase();
    if (!Number.isInteger(proxy.port) || proxy.port < 1 || proxy.port > 65535 || parsed.hash || parsed.username || parsed.password) return null;
    if (mode !== "web") {
      if (mode === "telegram") {
        if (parsed.protocol !== "https:" || parsed.hostname !== "t.me" || parsed.port || parsed.pathname !== "/proxy") return null;
        if ([...parsed.searchParams.keys()].some(key => !["server", "port", "secret"].includes(key))) return null;
      } else if (parsed.protocol !== "tg:" || parsed.hostname !== "proxy" || parsed.pathname) return null;
      for (const key of ["server", "port", "secret"]) if (parsed.searchParams.getAll(key).length !== 1) return null;
      const secret = parsed.searchParams.get("secret");
      if (parsed.searchParams.get("server").replace(/^\[|\]$/g, "").toLowerCase() !== server || parsed.searchParams.get("port") !== String(proxy.port)) return null;
      if (!/^[A-Za-z0-9_+/=-]{16,600}$/.test(secret)) return null;
      if (mode === "telegram") {
        const identity = secretIdentity(secret);
        if (identity === null || secretIdentity(proxy.secret) !== identity) return null;
      } else if (proxy.secret != null && proxy.secret !== secret) return null;
    } else {
      if (!Object.hasOwn(protocolNames, proxy.protocol) || parsed.protocol !== `${proxy.protocol}:` || parsed.search || !["", "/"].includes(parsed.pathname)) return null;
      const port = parsed.port ? Number(parsed.port) : ({http: 80, https: 443}[proxy.protocol] || 0);
      if (parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase() !== server || port !== proxy.port) return null;
    }
    return text;
  } catch { return null; }
}

async function copyText(text, button) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
    const label = button.textContent;
    button.textContent = "Copied";
    status("Copied to clipboard.");
    setTimeout(() => { if (button.isConnected) button.textContent = label; }, 1800);
  } catch {
    byId("copy-text").value = text;
    const dialog = byId("copy-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    byId("copy-text").focus();
    byId("copy-text").select();
  }
}

function node(tag, text = "", className = "") {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function resultCard(proxy) {
  const item = node("li", "", "result-card");
  const head = node("div", "", "result-head");
  head.append(node("span", `#${proxy.rank}`, "rank"));
  const identity = node("div", "", "identity");
  const server = String(proxy.server);
  identity.append(node("b", `${server.includes(":") ? `[${server}]` : server}:${proxy.port}`, "endpoint"));
  identity.append(node("span", mode === "mtproto" ? "MTProto · response verified" : mode === "telegram" ? "MTProto · HTTPS share link" : protocolNames[proxy.protocol] || "HTTP / SOCKS proxy", "protocol-label"));
  head.append(identity);
  item.append(head);
  const measures = node("div", "", "result-measures");
  for (const [label, value] of [["Latency", Number.isFinite(proxy.avg_latency_ms) ? `${Math.round(proxy.avg_latency_ms)} ms` : "—"], ["Success", percentage(proxy.success_rate)], ["Score", percentage(proxy.score)]]) {
    const measure = node("div", "", "measure");
    measure.append(node("span", label), node("strong", value));
    measures.append(measure);
  }
  item.append(measures, node("p", `Source: ${proxy.source}`, "result-source"));
  const connection = safeConnection(proxy);
  if (connection) {
    const actions = node("div", "", "result-actions");
    if (mode !== "web") {
      const link = node("a", mode === "telegram" ? "Open Telegram link" : "Connect Telegram", "button primary");
      link.href = connection;
      link.rel = "noreferrer";
      actions.append(link);
    }
    const copy = node("button", mode === "mtproto" ? "Copy link" : mode === "telegram" ? "Copy HTTPS link" : "Copy proxy URI", mode !== "web" ? "button quiet" : "button primary");
    copy.type = "button";
    copy.addEventListener("click", () => copyText(connection, copy));
    actions.append(copy);
    item.append(actions);
    const details = node("details", "", "connection-details");
    details.append(node("summary", "View connection text"), node("code", connection));
    item.append(details);
  } else item.append(node("p", "Connection text unavailable for this result.", "muted"));
  return item;
}

function filteredRows(snapshot) {
  const search = byId("search").value.trim().toLowerCase();
  const protocol = byId("protocol").value;
  const sort = byId("sortBy").value;
  const rows = snapshot.rows.filter(proxy => (!search || `${proxy.server} ${proxy.source}`.toLowerCase().includes(search)) && (mode !== "web" || protocol === "all" || proxy.protocol === protocol));
  if (sort !== "score") rows.sort((first, second) => {
    const a = first[sort]; const b = second[sort];
    if (!Number.isFinite(a)) return Number.isFinite(b) ? 1 : first.rank - second.rank;
    if (!Number.isFinite(b)) return -1;
    const difference = sort === "avg_latency_ms" ? a - b : b - a;
    return difference || first.rank - second.rank;
  });
  return {total: rows.length, rows: rows.slice(0, Number(byId("limit").value))};
}

function renderResults(current) {
  const visible = filteredRows(current);
  byId("results").replaceChildren(...visible.rows.map(resultCard));
  byId("count-displayed").textContent = count(visible.rows.length);
  byId("result-count").textContent = `Showing ${visible.rows.length} of ${visible.total} eligible results`;
  byId("empty-state").hidden = visible.rows.length > 0;
  byId("clear-filters").hidden = true;
  let title = "No scan yet";
  let description = `Choose ${modeLabels[mode]} in the terminal, run a scan, then refresh this page.`;
  if (current.state === "corrupt") {
    title = "Saved results need a new scan";
    description = current.message;
  } else if (current.rows.length && !visible.rows.length) {
    title = "No matches for these filters";
    description = "Try another endpoint or source, or clear the search and protocol filter.";
    byId("clear-filters").hidden = false;
  } else if (current.state === "ready") {
    title = current.summary.interrupted ? "No verified results before this scan stopped" : "No verified results in this scan";
    description = current.summary.interrupted ? "Only tested candidates contributed results. Skipped candidates were not tested; run another scan to check them." : "Check source health and failure details above. Try another scan or network; failed candidates are never used to fill the top ten.";
  } else if (current.state === "legacy") {
    title = "No saved legacy results";
    description = "Run a fresh MTProto scan to create results with current verification and scan counts.";
  }
  byId("empty-title").textContent = title;
  byId("empty-description").textContent = description;
}

function renderHealth(current) {
  const reports = current.source_reports;
  const reached = reports.filter(report => report.status !== "error").length;
  byId("source-summary").textContent = reports.length ? `Sources reached: ${reached} / ${reports.length}` : "Source health · no report";
  byId("source-list").replaceChildren(...reports.map(report => node("li", `${report.name} · ${report.status} · ${report.count} candidates${report.error ? ` · ${report.error}` : ""}`, report.status === "error" ? "source-error" : "")));
  if (!reports.length) byId("source-list").append(node("li", "Source reports appear after a v3 scan."));
  const failures = Object.entries(current.summary.failure_counts || {});
  byId("failure-summary").textContent = failures.length ? `Failure details · ${failures.length} categories` : "Failure details · none reported";
  byId("failure-list").replaceChildren(...failures.map(([reason, total]) => node("li", `${reason.replaceAll("_", " ")}: ${count(total)}`)));
  if (!failures.length) byId("failure-list").append(node("li", "No failure details recorded for this snapshot."));
}

function render(current, force = false) {
  if (!current) {
    current = {state: "missing", message: "No scan snapshot loaded.", rows: [], summary: {}, source_reports: []};
  }
  const summary = current.summary;
  const interrupted = summary.interrupted === true;
  byId("tested-description").textContent = interrupted ? "Candidates with samples" : "Completed checks";
  byId("partial-note").hidden = !interrupted;
  byId("partial-note").textContent = `Stopped scan / partial results. Tested ${count(summary.tested)} of ${count(summary.selected)} selected candidates; ${count(summary.skipped)} skipped. These ranks cover the tested candidates.`;
  byId("scan-time-label").textContent = interrupted ? "SCAN STOPPED AT" : "LAST COMPLETED SCAN";
  byId("results-title").textContent = interrupted ? "Verified partial results" : "Top verified results";
  for (const key of ["selected", "tested", "verified"]) byId(`count-${key}`).textContent = count(summary[key]);
  const timestamp = summary.completed_at ? new Date(summary.completed_at) : null;
  if (timestamp && !Number.isNaN(timestamp.getTime())) {
    byId("scan-time").textContent = timestamp.toLocaleString();
    byId("scan-time").dateTime = summary.completed_at;
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp.getTime()) / 60000));
    byId("freshness").textContent = minutes < 1 ? "Just completed" : minutes < 60 ? `${minutes} min ago` : `${Math.floor(minutes / 60)} hours ago`;
    byId("freshness").className = minutes >= 60 ? "pill warning" : "pill neutral";
    byId("scan-meta").textContent = `Requested: ${summary.requested_count == null ? "ALL" : count(summary.requested_count)} · Collected: ${count(summary.collected)} · Eligible: ${count(summary.eligible)} · Skipped: ${count(summary.skipped ?? (summary.selected - summary.tested))}`;
  } else {
    byId("scan-time").textContent = current.state === "legacy" ? "Scan time unknown · v2 results" : "No completed scan";
    byId("scan-time").removeAttribute("datetime");
    byId("freshness").textContent = current.state === "corrupt" ? "Invalid snapshot" : current.state === "legacy" ? "Legacy snapshot" : "Waiting for results";
    byId("freshness").className = current.state === "corrupt" ? "pill warning" : "pill neutral";
    byId("scan-meta").textContent = current.message || "Start a scan in the terminal to see its results here.";
  }
  // v3 scans have unique run IDs. Legacy/custom snapshots need their contents
  // compared because two completed scans can share a timestamp and row count.
  const revision = typeof summary.run_id === "string" && summary.run_id ? summary.run_id : JSON.stringify({summary, rows: current.rows, source_reports: current.source_reports});
  const identity = `${mode}:${current.state}:${revision}`;
  if (force || identity !== renderedIdentity) {
    renderResults(current);
    renderHealth(current);
    renderedIdentity = identity;
  }
}

function schedule() {
  clearTimeout(pollTimer);
  if (!paused && !document.hidden) pollTimer = setTimeout(() => refresh(false), 15000);
}

async function refresh(force = true) {
  clearTimeout(pollTimer);
  if (activeRequest) activeRequest.abort();
  const controller = new AbortController();
  activeRequest = controller;
  const requestMode = mode;
  const ticket = ++generation;
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 10000);
  byId("refresh").disabled = true;
  byId("refresh").setAttribute("aria-busy", "true");
  status("Refreshing saved scan results…");
  try {
    const response = await fetch(`/api/snapshot?mode=${requestMode}`, {cache: "no-store", signal: controller.signal});
    if (!response.ok) throw new Error(`Dashboard returned HTTP ${response.status}`);
    const current = await response.json();
    if (ticket !== generation || requestMode !== mode) return;
    if (current.mode !== requestMode || !["ready", "missing", "legacy", "corrupt"].includes(current.state) || !Array.isArray(current.rows) || !current.summary || !Array.isArray(current.source_reports)) throw new Error("Invalid dashboard response");
    snapshots.set(requestMode, current);
    connected = true;
    render(current, force);
    byId("connection-state").textContent = "Local dashboard";
    byId("connection-state").className = "pill";
    status(current.state === "ready" ? current.summary.interrupted ? `Stopped scan / partial results: ${count(current.summary.tested)} tested, ${count(current.summary.skipped)} skipped. Proxies can change after verification.` : "Showing a completed snapshot. Proxies can change after verification." : current.message, current.state === "corrupt");
  } catch (error) {
    if (ticket !== generation || (!timedOut && error.name === "AbortError")) return;
    connected = false;
    byId("connection-state").textContent = "Session unavailable";
    byId("connection-state").className = "pill warning";
    render(snapshots.get(mode), false);
    status(`${timedOut ? "Refresh timed out" : error.message}. ${snapshots.has(mode) ? "Showing previously loaded results; they may be stale." : "No results loaded."} Keep the terminal dashboard session running and retry Refresh.`, true);
  } finally {
    clearTimeout(timeout);
    if (ticket === generation) {
      activeRequest = null;
      byId("refresh").disabled = false;
      byId("refresh").removeAttribute("aria-busy");
      schedule();
    }
  }
}

function selectMode(next, focus = false) {
  if (!modes.includes(next)) return;
  mode = next;
  renderedIdentity = "";
  for (const value of modes) {
    const tab = byId(`tab-${value}`);
    tab.setAttribute("aria-selected", String(value === mode));
    tab.tabIndex = value === mode ? 0 : -1;
  }
  if (focus) byId(`tab-${mode}`).focus();
  byId("proxy-panel").setAttribute("aria-labelledby", `tab-${mode}`);
  byId("mode-label").textContent = mode === "mtproto" ? "TELEGRAM TRANSPORT" : mode === "telegram" ? "TELEGRAM HTTPS SHARE LINKS" : "HTTP / SOCKS CONNECTIVITY";
  byId("mode-title").textContent = mode === "mtproto" ? "Your best MTProto connections" : mode === "telegram" ? "Your best Telegram Web Links" : "Your best HTTP / SOCKS proxies";
  byId("mode-description").textContent = mode === "mtproto" ? "Verified MTProto responses, ranked by score with latency breaking ties." : mode === "telegram" ? "Verified MTProto proxies shared as HTTPS t.me/proxy links with their original server, port and secret." : "HTTP, HTTPS and SOCKS proxies checked through a certificate-verified HTTPS request.";
  byId("verification-note").textContent = mode !== "web" ? "A matched MTProto response checks reachability at scan time; later connectivity can change." : "Verification checks the configured HTTPS origin at scan time. It does not guarantee access to every website.";
  byId("scan-command").textContent = `python main.py best10 --mode ${mode} --count ALL`;
  byId("protocol-field").hidden = mode !== "web";
  byId("search").value = "";
  byId("protocol").value = "all";
  byId("sortBy").value = "score";
  history.replaceState(null, "", `?mode=${mode}`);
  render(snapshots.get(mode), true);
  refresh(true);
}

for (const value of modes) {
  const tab = byId(`tab-${value}`);
  tab.addEventListener("click", () => { if (value !== mode) selectMode(value); });
  tab.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = (modes.indexOf(mode) + (event.key === "ArrowLeft" ? -1 : 1) + modes.length) % modes.length;
      selectMode(event.key === "Home" ? modes[0] : event.key === "End" ? modes.at(-1) : modes[next], true);
    }
  });
}
byId("refresh").addEventListener("click", () => refresh(true));
byId("pause").addEventListener("click", () => {
  paused = !paused;
  byId("pause").setAttribute("aria-pressed", String(paused));
  byId("pause").textContent = paused ? "Resume updates" : "Pause updates";
  clearTimeout(pollTimer);
  status(paused ? "Automatic updates paused. Refresh still works." : connected ? "Automatic updates resumed." : "Updates resumed. Checking the local dashboard…");
  if (!paused) refresh(false);
});
for (const key of ["search", "sortBy", "protocol", "limit"]) byId(key).addEventListener(key === "search" ? "input" : "change", () => render(snapshots.get(mode), true));
byId("clear-filters").addEventListener("click", () => {
  byId("search").value = "";
  byId("protocol").value = "all";
  render(snapshots.get(mode), true);
  byId("search").focus();
});
byId("copy-command").addEventListener("click", event => copyText(byId("scan-command").textContent, event.currentTarget));
document.addEventListener("visibilitychange", () => {
  clearTimeout(pollTimer);
  if (document.hidden) { if (activeRequest) activeRequest.abort(); }
  else if (!paused) refresh(false);
});
selectMode(mode);
