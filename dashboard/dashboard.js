"use strict";
const byId = id => document.getElementById(id);
const modes = ["mtproto", "telegram", "webproxy", "web"];
const visibleModes = ["mtproto", "webproxy", "web"];
let linkFormat = "tg";
const modeLabels = {mtproto: "MTProto", telegram: "MTProto", webproxy: "Telegram WEB · آزمایشی", web: "HTTP / SOCKS"};
const protocolNames = {http: "HTTP · HTTPS tunnel", https: "HTTPS · TLS to proxy", socks4: "SOCKS4", socks5: "SOCKS5"};
const snapshots = new Map();
let mode = modes.includes(new URLSearchParams(location.search).get("mode")) ? new URLSearchParams(location.search).get("mode") : "mtproto";
let paused = false;
let pollTimer;
let expiryTimer;
let activeRequest;
let generation = 0;
let renderedIdentity = "";
let connected = false;
const explicitNetwork = new URLSearchParams(location.search).has("network");
let networkScoped = explicitNetwork;
let networkChangedByUser = false;
let network = new URLSearchParams(location.search).get("network") || "default";
if (!/^[A-Za-z0-9_-]{1,32}$/.test(network)) network = "default";
network = network.toLowerCase();
let controlToken = null;
let controlJob = null;
let controlBusy = false;
let controlInitialized = false;
const count = value => Number.isInteger(value) && value >= 0 ? value.toLocaleString() : "—";
const percentage = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
const stages = {prefilter: "بررسی اولیهٔ دسترسی", baseline: "آزمون پایهٔ جلسهٔ تلگرام", finalists: "بازآزمایی پایدار نامزدهای نهایی", complete: "آزمون کامل شد", stopped: "آزمون متوقف شد"};

function selectedNetwork(current) {
  const value = current?.network ?? current?.summary?.network ?? network;
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(value) ? value : "default";
}

function controlFeedback(text, error = false) {
  byId("control-feedback").textContent = text;
  byId("control-feedback").classList.toggle("error", error);
}

function renderControl() {
  const active = controlJob && ["running", "stopping"].includes(controlJob.state);
  const scoped = controlJob?.mode === mode && controlJob?.network === network;
  for (const id of ["control-start", "control-resume", "control-recheck"]) byId(id).disabled = !controlToken || controlBusy || active;
  byId("control-stop").disabled = !controlToken || controlBusy || !active || !scoped || controlJob.state === "stopping";
  byId("control-state").textContent = controlJob ? `${modeLabels[controlJob.mode] || "آزمون"} · ${controlJob.network} · ${{running: "در حال اجرا", stopping: "در حال ذخیره", stopped: "متوقف", complete: "کامل", failed: "ناموفق"}[controlJob.state]}` : "آماده";
  byId("control-count-field").hidden = byId("control-scope").value !== "count";
}

async function refreshControl() {
  const pending = new AbortController();
  const timeout = setTimeout(() => pending.abort(), 10000);
  try {
    const response = await fetch("/api/control", {cache: "no-store", credentials: "same-origin", signal: pending.signal});
    if (!response.ok) throw new Error("کنترل محلی در دسترس نیست");
    const result = await response.json();
    if (result.enabled !== true || typeof result.token !== "string" || !/^[A-Za-z0-9_-]{40,100}$/.test(result.token)) {
      controlToken = null;
      byId("scan-controls").hidden = true;
      byId("personal-library").hidden = true;
      return;
    }
    controlToken = result.token;
    byId("scan-controls").hidden = false;
    byId("personal-library").hidden = false;
    byId("terminal-title").textContent = "فرمان‌های ترمینال نیز در دسترس هستند.";
    byId("terminal-description").textContent = "از کنترل‌های بالا یا فرمان ترمینال استفاده کنید و این نشست را باز نگه دارید.";
    if (!controlInitialized) {
      if (!explicitNetwork && !networkChangedByUser && typeof result.network === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(result.network)) {
        const initialNetwork = result.network.toLowerCase();
        networkScoped = true;
        if (network !== initialNetwork) { network = initialNetwork; snapshots.clear(); refresh(true); }
      }
      byId("control-network").value = network;
      controlInitialized = true;
    }
    const previous = controlJob;
    const candidate = result.job;
    controlJob = candidate && modes.includes(candidate.mode) && typeof candidate.network === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(candidate.network) && ["running", "stopping", "stopped", "complete", "failed"].includes(candidate.state) ? candidate : null;
    if (controlJob && (previous?.id !== controlJob.id || previous?.state !== controlJob.state)) controlFeedback({running: "آزمون آغاز شده و در حال اجرا است.", stopping: "توقف درخواست شد؛ منتظر ذخیرهٔ نتایج بمانید.", stopped: "متوقف شد؛ می‌توانید نتایج را ببینید یا اسکن ذخیره‌شده را ادامه دهید.", complete: "آزمون کامل شد؛ نتایج زیر را تازه‌سازی کنید.", failed: "آزمون کامل نشد؛ وضعیت ترمینال را بررسی و دوباره تلاش کنید."}[controlJob.state], controlJob.state === "failed");
    renderControl();
    refreshLibrary();
  } catch {
    if (controlToken) controlFeedback("کنترل محلی پاسخ نمی‌دهد؛ ترمینال را باز نگه دارید و دوباره امتحان کنید.", true);
  } finally { clearTimeout(timeout); }
}

function controlPayload(action) {
  const value = byId("control-network").value.trim().toLowerCase();
  if (!/^[a-z0-9_-]{1,32}$/.test(value) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/.test(value)) throw new Error("نام شبکه باید ۱ تا ۳۲ حرف انگلیسی، عدد، خط تیره یا زیرخط باشد.");
  const payload = {action, mode, network: value};
  if (action === "start") {
    const scope = byId("control-scope").value;
    if (scope === "count") {
      const amount = Number(byId("control-count").value);
      if (!Number.isInteger(amount) || amount < 1 || amount > 1000000) throw new Error("تعداد باید یک عدد صحیح بین ۱ و ۱۰۰۰۰۰۰ باشد.");
      payload.count = amount;
    } else if (["3", "10", "20"].includes(scope)) payload.minutes = Number(scope);
    else if (scope !== "all") throw new Error("ALL، تعداد دلخواه یا زمان آزمون را انتخاب کنید.");
  }
  if (action === "resume") payload.command = byId("control-resume-kind").value;
  return payload;
}

async function sendControl(action) {
  if (!controlToken || controlBusy) return;
  let payload;
  try { payload = controlPayload(action); }
  catch (error) { controlFeedback(error.message, true); return; }
  controlBusy = true;
  const pending = new AbortController();
  const timeout = setTimeout(() => pending.abort(), 10000);
  renderControl();
  controlFeedback(action === "stop" ? "درخواست توقف و ذخیره…" : "در حال ارسال درخواست آزمون…");
  try {
    const response = await fetch("/api/control", {method: "POST", credentials: "same-origin", cache: "no-store", signal: pending.signal, headers: {"Content-Type": "application/json", "X-Revmamad-Token": controlToken}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.error === "string" ? result.error : "درخواست آزمون پذیرفته نشد.");
    networkScoped = true;
    networkChangedByUser = true;
    if (network !== payload.network) { network = payload.network; snapshots.clear(); }
    controlFeedback(action === "stop" ? "توقف درخواست شد؛ ذخیرهٔ آزمون‌های کامل‌شده در حال انجام است." : "آزمون شروع شد؛ روند و نتایج کامل‌شده در پایین نمایش داده می‌شوند.");
    await refreshControl();
    refresh();
  } catch (error) {
    controlFeedback(error.name === "AbortError" ? "پاسخ دیر رسید؛ پیش از تکرار، وضعیت را تازه‌سازی کنید چون ممکن است اسکن آغاز شده باشد." : error.message || "درخواست ارسال نشد؛ پیش از تکرار، کنترل‌ها را تازه‌سازی کنید.", true);
  } finally { clearTimeout(timeout); controlBusy = false; renderControl(); }
}

function terminalCommand(current, resume = false, recheck = false) {
  const network = selectedNetwork(current);
  const command = recheck || resume && (current?.progress?.command === "recheck" || current?.summary?.command === "recheck") ? "recheck" : "best10";
  return `python main.py ${command} --mode ${mode}${command === "best10" ? " --count ALL" : ""} --network ${network}${resume ? " --resume" : ""}`;
}

function validProgress(current) {
  const value = current.progress;
  if (!value || value.version !== 1 || value.mode !== mode || value.network !== selectedNetwork(current) || !Object.hasOwn(stages, value.stage)) return null;
  if (![value.completed, value.total, value.qualified].every(n => Number.isInteger(n) && n >= 0) || value.completed > value.total || value.qualified > value.total) return null;
  if (typeof value.active !== "boolean" || typeof value.resumable !== "boolean") return null;
  if (value.remaining_seconds != null && (!Number.isFinite(value.remaining_seconds) || value.remaining_seconds < 0)) return null;
  const age = Date.now() - new Date(value.updated_at).getTime();
  return Number.isFinite(age) && age >= -60000 && age <= (value.recovery_needed ? 7 * 86400000 : value.active ? 180000 : 86400000) ? value : null;
}

function renderActivity(current) {
  const network = selectedNetwork(current);
  const progress = validProgress(current);
  byId("network-label").textContent = `شبکه: ${network}`;
  byId("scan-command").textContent = terminalCommand(current);
  byId("copy-resume").hidden = !(progress?.resumable && !progress.active);
  byId("scan-progress").max = progress?.total || 1;
  byId("scan-progress").value = progress?.completed || 0;
  byId("progress-label").textContent = progress?.recovery_needed ? "اجرای قبلی پایان یافته؛ ادامهٔ امن از دادهٔ ذخیره‌شده" : progress ? stages[progress.stage] : "آزمون تازه‌ای گزارش نشده";
  const remaining = progress?.remaining_seconds;
  const estimate = remaining == null ? "زمان باقی‌مانده هنوز تخمین زده نشده" : `حدود ${Math.ceil(remaining / 60)} دقیقه باقی‌مانده (تخمینی)`;
  byId("progress-meta").textContent = progress
    ? `${count(progress.completed)} / ${count(progress.total)} در این مرحله · ${count(progress.qualified)} واجد شرایط · ${progress.active ? estimate : progress.resumable ? "اسکن ذخیره‌شده را ادامه دهید؛ شواهد منقضی دوباره اندازه‌گیری می‌شوند." : "آزمون تازه را از کنترل‌ها یا ترمینال شروع کنید."}`
    : current.progress_state === "stale" ? "پیام فعالیت قدیمی است؛ ممکن است ترمینال متوقف شده باشد. زمان نتایج ذخیره‌شده تغییری نکرده است." : "روند آزمون جاری اینجا دیده می‌شود؛ نتایج پایین مربوط به زمان ثبت‌شدهٔ خودشان هستند.";
  byId("scan-progress").setAttribute("aria-valuetext", byId("progress-label").textContent + ": " + (progress ? `${progress.completed} of ${progress.total}` : "فعالیت تازه‌ای نیست"));
  const observations = Array.isArray(current.history) ? current.history.filter(row => row && typeof row === "object" && row.mode === mode && row.network === network && typeof row.server === "string").slice(0, 20) : [];
  byId("history-count").textContent = observations.length ? `· ${observations.length} سرور` : "";
  byId("history-list").replaceChildren(...observations.map(row => {
    const tested = new Date(row.last_tested * 1000);
    const date = Number.isNaN(tested.getTime()) ? "زمان نامشخص" : tested.toLocaleString();
    return node("li", `${String(row.server)}:${row.port} · ${date} · آخرین آزمون ${count(row.successes)} / ${count(row.attempts)} موفق · p95 ${Number.isFinite(row.p95_ms) ? Math.round(row.p95_ms) + " ms" : "نامشخص"} · ${row.qualified === true ? "در آن زمان واجد شرایط" : "تأیید نشد"} · تاریخچه ${count(row.historical_successes)} / ${count(row.historical_attempts)} در ${count(row.run_count)} اجرا`);
  }));
  if (!observations.length) byId("history-list").append(node("li", current.history_state === "corrupt" ? "تاریخچه قابل خواندن نبود؛ با آزمون تازه به‌روزرسانی می‌شود." : "برای این گروه و شبکه تاریخچه‌ای ذخیره نشده است."));
}

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

function strictQualified(row) {
  const policy = row.strict_policy;
  if (row.verification_profile !== "telegram_session" || row.strict_eligible !== true || !policy || typeof policy !== "object" || Array.isArray(policy)) return false;
  const validDCs = values => Array.isArray(values) && values.every(value => Number.isInteger(value) && value >= 1 && value <= 5) && new Set(values).size === values.length;
  if (!validDCs(policy.required_dc_ids) || !validDCs(row.dc_ids_verified) || new Set(policy.required_dc_ids).size < 2 || !policy.required_dc_ids.every(value => row.dc_ids_verified.includes(value))) return false;
  if (!Number.isInteger(policy.min_successes) || policy.min_successes < 3 || !Number.isInteger(policy.min_ping_count) || policy.min_ping_count < 3 || !Number.isFinite(policy.min_success_rate) || policy.min_success_rate < .8 || policy.min_success_rate > 1) return false;
  if (![row.attempts, row.successes, row.strict_successes].every(Number.isInteger) || row.strict_successes < policy.min_successes || row.strict_successes > row.successes || row.successes > row.attempts) return false;
  const observed = row.strict_successes / row.attempts;
  if (!Number.isFinite(row.success_rate) || Math.abs(row.success_rate - row.successes / row.attempts) > .0001) return false;
  if (observed < policy.min_success_rate || !Number.isInteger(row.ping_count) || row.ping_count < row.strict_successes * policy.min_ping_count) return false;
  if (![row.telegram_median_ms, row.telegram_p95_ms, row.telegram_jitter_ms, row.reliability_lower_bound].every(value => Number.isFinite(value) && value >= 0)) return false;
  if (!Number.isFinite(policy.max_p95_ms) || policy.max_p95_ms <= 0 || !Number.isFinite(policy.max_jitter_ms) || policy.max_jitter_ms <= 0) return false;
  const z2 = 1.96 ** 2;
  const wilson = (observed + z2 / (2 * row.attempts) - 1.96 * Math.sqrt((observed * (1 - observed) + z2 / (4 * row.attempts)) / row.attempts)) / (1 + z2 / row.attempts);
  return row.telegram_median_ms <= row.telegram_p95_ms && row.telegram_p95_ms <= policy.max_p95_ms && row.telegram_jitter_ms <= policy.max_jitter_ms && Math.abs(row.reliability_lower_bound - wilson) <= .0001;
}

function webSecretIdentity(value, path = false) {
  if (typeof value !== "string") return null;
  let marked = false;
  if (/^[A-Za-z0-9_-]+$/.test(value)) {
    try {
      const raw = Array.from(atob(value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4)), c => c.charCodeAt(0));
      if (raw[0] === 0x70 && raw.length >= 17) {
        const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let encoded = "", bits = 0, pending = 0;
        for (const byte of raw) {
          pending = (pending << 8) | byte;
          bits += 8;
          while (bits >= 6) { bits -= 6; encoded += alphabet[(pending >> bits) & 63]; }
        }
        if (bits) encoded += alphabet[(pending << (6 - bits)) & 63];
        if (encoded !== value) return null;
        marked = true;
        value = raw.slice(1).map(byte => byte.toString(16).padStart(2, "0")).join("");
      }
    } catch { return null; }
  }
  if (path && !marked) return null;
  const identity = secretIdentity(value);
  return identity && (identity.length === 32 || identity.length === 34 && identity.startsWith("dd")) ? identity : null;
}

function safeWebProxyConnection(proxy, profile, text = proxy.web_link) {
  if (profile !== "telegram_session" || !strictQualified(proxy) || proxy.protocol !== "webproxy" || proxy.port !== 443) return null;
  if (typeof text !== "string" || text.length > 2048 || /[\u0000-\u0020]/.test(text)) return null;
  try {
    const parsed = new URL(text);
    const https = parsed.protocol === "https:" && ["t.me", "telegram.me"].includes(parsed.hostname) && !parsed.port && parsed.pathname === "/webproxy";
    const direct = parsed.protocol === "tg:" && parsed.hostname === "webproxy" && !parsed.port && !parsed.pathname;
    if (!(https || direct) || parsed.hash || parsed.username || parsed.password) return null;
    if ([...parsed.searchParams.keys()].some(key => !["server", "secret"].includes(key)) || ["server", "secret"].some(key => parsed.searchParams.getAll(key).length !== 1)) return null;
    const server = parsed.searchParams.get("server");
    const parts = server.split("/");
    const host = parts.shift().toLowerCase();
    const path = parts.join("/");
    if (!host.includes(".") || host.length > 253 || /^[\d.]+$/.test(host) || !host.split(".").every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
    if (parts.length && (!path || path.length > 128 || !/^[A-Za-z0-9][A-Za-z0-9_-]*(?:\/[A-Za-z0-9][A-Za-z0-9_-]*)*$/.test(path))) return null;
    if (host + (parts.length ? "/" + path : "") !== proxy.server) return null;
    const secret = webSecretIdentity(parsed.searchParams.get("secret"), !!path);
    return secret && secret === webSecretIdentity(proxy.secret) ? text : null;
  } catch { return null; }
}

function safeConnection(proxy, profile = "telegram_session") {
  if (!evidenceActionable(proxy)) return null;
  if (mode === "webproxy") return safeWebProxyConnection(proxy, profile) && safeWebProxyConnection(proxy, profile, proxy.tg_link);
  if (mode !== "web" && (profile !== "telegram_session" || !strictQualified(proxy))) return null;
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

function qualityReason(proxy, strict) {
  if (mode === "web") return proxy.success_rate >= 1 ? "همهٔ درخواست‌های وب آزموده‌شده موفق بودند؛ دسترسی به سایت‌ها ممکن است تغییر کند." : "بعضی درخواست‌های وب ناموفق بودند؛ پیش از استفاده دوباره آزمایش کنید.";
  if (!strict) return "برای پیشنهاد این اتصال، شواهد دقیق‌تری از جلسهٔ تلگرام لازم است.";
  if (proxy.strict_successes < proxy.attempts) return "با یک شکست اتصال مجدد تأیید شده؛ بازآزمایی برای بررسی ثبات فعلی مفید است.";
  if (proxy.telegram_p95_ms > 700) return "در این نمونهٔ کوتاه قابل‌اتکا بود، اما پاسخ تلگرام کندتر است.";
  if (proxy.telegram_jitter_ms > 150) return "همهٔ جلسه‌های آزموده‌شده موفق بودند، اما پینگ نوسان داشت.";
  return "در این نمونهٔ کوتاه، پاسخ تلگرام باثبات و اتصال‌های مجدد موفق بودند.";
}

async function copyText(text, button) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
    const label = button.textContent;
    button.textContent = "کپی شد";
    status("متن کپی شد.");
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

function resultCard(proxy, profile = proxy.verification_profile || "transport") {
  const strict = mode !== "web" && profile === "telegram_session" && strictQualified(proxy);
  const item = node("li", "", "result-card");
  const head = node("div", "", "result-head");
  head.append(node("span", `#${proxy.rank}`, "rank"));
  const identity = node("div", "", "identity");
  const server = String(proxy.server);
  identity.append(node("b", mode === "webproxy" ? server : `${server.includes(":") ? `[${server}]` : server}:${proxy.port}`, "endpoint"));
  identity.append(node("span", mode === "web" ? protocolNames[proxy.protocol] || "HTTP / SOCKS proxy" : strict ? mode === "webproxy" ? "حامل WEB دارای گواهی معتبر · آزمون دقیق تلگرام و پینگ رمز‌شده" : "آزمون دقیق تلگرام · هویت سرور و پینگ رمز‌شده" : "فقط آزمون اتصال اولیه؛ بازآزمایی دقیق لازم است", "protocol-label"));
  head.append(identity);
  item.append(head);
  const measures = node("div", "", "result-measures");
  const milliseconds = value => Number.isFinite(value) ? `${Math.round(value)} ms` : "—";
  const passes = Number.isInteger(proxy.attempts) && Number.isInteger(strict ? proxy.strict_successes : proxy.successes) ? `${strict ? proxy.strict_successes : proxy.successes} / ${proxy.attempts}` : "ثبت نشده";
  const measurements = mode === "web"
    ? [["تأخیر", milliseconds(proxy.avg_latency_ms)], ["موفقیت", percentage(proxy.success_rate)], ["امتیاز", percentage(proxy.score)]]
    : [[strict ? "Telegram RTT p95" : "مدت آزمون", milliseconds(strict ? proxy.telegram_p95_ms : proxy.avg_latency_ms)], [strict ? "جلسهٔ موفق" : "آزمون موفق", passes], [strict ? "امتیاز ترکیبی" : "امتیاز تشخیصی", percentage(proxy.score)]];
  for (const [label, value] of measurements) {
    const measure = node("div", "", "measure");
    measure.append(node("span", label), node("strong", value));
    measures.append(measure);
  }
  item.append(measures);
  item.append(node("p", qualityReason(proxy, strict), "quality-note"));
  if (mode !== "web") {
    if (strict) {
      item.append(node("p", `مدت آزمون: ${milliseconds(proxy.avg_latency_ms)} · میانهٔ پینگ تلگرام: ${milliseconds(proxy.telegram_median_ms)} · نوسان پینگ: ${milliseconds(proxy.telegram_jitter_ms)} · ${proxy.ping_count} پاسخ پینگ معتبر · DCs ${proxy.dc_ids_verified.join(", ")}`, "measurement-note"));
      item.append(node("p", `کران پایین اطمینان: ${percentage(proxy.reliability_lower_bound)} (Wilson 95%، ${proxy.attempts} اتصال مجدد). این نمونهٔ کوتاه، دسترسی بلندمدت را ثابت نمی‌کند.`, "measurement-note"));
    } else {
      item.append(node("p", Number.isInteger(proxy.successes) && proxy.successes >= 2 && Number.isFinite(proxy.stability) ? `ثبات زمان آزمون: ${percentage(proxy.stability)} · فقط شواهد تشخیصی.` : "برای سنجش ثبات تأخیر، نمونه کافی نیست.", "measurement-note"));
    }
  }
  item.append(node("p", `منبع: ${proxy.source} · ${count(proxy.source_memberships?.length || 1)} عضویت منبع (غیرجمع‌پذیر)`, "result-source"));
  const state = evidenceState(proxy);
  item.append(node("p", `وضعیت: ${state === "fresh" ? "کامل و تازه" : state === "provisional" ? "موقت؛ بازآزمایی لازم" : state === "expired" ? "منقضی؛ بازآزمایی لازم" : "زمان اعتبار نامشخص؛ بازآزمایی لازم"} · شبکه: ${network}`, "status-badge"));
  const observedAt = Number.isFinite(proxy.latest_observed_at) ? new Date(proxy.latest_observed_at * 1000).toLocaleString("fa-IR") : "ثبت نشده";
  const expiresAt = Number.isFinite(proxy.evidence_expires_at) ? new Date(proxy.evidence_expires_at * 1000).toLocaleString("fa-IR") : "نامشخص";
  item.append(node("p", `آخرین نمونه: ${observedAt} · انقضا: ${expiresAt} · سیاست: ${proxy.ranking_policy === "v2-experimental" ? "v2 آزمایشی" : "v1"}`, "measurement-note"));
  const connection = safeConnection(proxy, profile);
  if (connection) {
    const actions = node("div", "", "result-actions");
    if (mode !== "web") {
      const link = node("a", mode === "telegram" ? "باز کردن لینک تلگرام" : mode === "webproxy" ? "اتصال Telegram WEB" : "اتصال به تلگرام", "button primary");
      link.href = shareConnection(connection);
      link.rel = "noreferrer";
      link.addEventListener("click", event => { if (!evidenceActionable(proxy)) { event.preventDefault(); status("اعتبار این نتیجه تمام شده؛ دوباره آزمون بگیرید.", true); render(snapshots.get(mode), true); } });
      actions.append(link);
    }
    const copy = node("button", mode === "mtproto" ? "کپی لینک" : mode === "telegram" ? "کپی لینک HTTPS" : mode === "webproxy" ? "کپی لینک HTTPS وب" : "کپی نشانی پروکسی", mode !== "web" ? "button quiet" : "button primary");
    copy.type = "button";
    copy.addEventListener("click", () => { if (evidenceActionable(proxy)) copyText(mode === "webproxy" ? proxy.web_link : shareConnection(connection), copy); else { status("اعتبار این نتیجه تمام شده؛ دوباره آزمون بگیرید.", true); render(snapshots.get(mode), true); } });
    actions.append(copy);
    if (controlToken) {
      const favourite = node("button", "افزودن به علاقه‌مندی", "button quiet");
      favourite.type = "button";
      favourite.addEventListener("click", () => libraryAction({action: "favourite_add", mode, network, rank: proxy.rank}));
      actions.append(favourite);
    }
    item.append(actions);
    const details = node("details", "", "connection-details");
    details.append(node("summary", "نمایش متن اتصال"), node("code", shareConnection(connection)));
    item.append(details);
    if (mode === "webproxy") item.append(node("p", "به نسخهٔ تلگرام سازگار با WEB نیاز دارد. لینک HTTPS ممکن است در مرورگر درست باز نشود؛ دکمهٔ اتصال، لینک مستقیم برنامه است.", "measurement-note"));
  } else item.append(node("p", mode === "web" ? "این نتیجه اتصال قابل‌استفادهٔ تازه ندارد." : "فقط برای مشاهده؛ پیش از استفاده آزمون دقیق و تازه بگیرید.", "muted"));
  return item;
}

function filteredRows(snapshot) {
  const search = byId("search").value.trim().toLowerCase();
  const protocol = byId("protocol").value;
  const sort = byId("sortBy").value;
  const rows = snapshot.rows.filter(proxy => (mode === "web" || snapshot.summary?.verification_profile !== "telegram_session" || strictQualified(proxy)) && (!search || `${proxy.server} ${proxy.source}`.toLowerCase().includes(search)) && (mode !== "web" || protocol === "all" || proxy.protocol === protocol));
  if (sort !== "score") rows.sort((first, second) => {
    const a = first[sort]; const b = second[sort];
    if (!Number.isFinite(a)) return Number.isFinite(b) ? 1 : first.rank - second.rank;
    if (!Number.isFinite(b)) return -1;
    const difference = sort.endsWith("_ms") ? a - b : b - a;
    return difference || first.rank - second.rank;
  });
  return {total: rows.length, rows: rows.slice(0, Math.min(10, Number(byId("limit").value) || 10))};
}

function renderResults(current) {
  const visible = filteredRows(current);
  byId("results").replaceChildren(...visible.rows.map(proxy => resultCard(proxy, current.summary?.verification_profile || (mode === "web" ? "web" : "transport"))));
  byId("count-displayed").textContent = count(visible.rows.length);
  byId("result-count").textContent = `نمایش ${visible.rows.length} از ${visible.total} نتیجه`;
  byId("empty-state").hidden = visible.rows.length > 0;
  byId("clear-filters").hidden = true;
  let title = "هنوز آزمونی انجام نشده";
  let description = `گروه ${modeLabels[mode]} را انتخاب و آزمون را شروع کنید.`;
  if (current.state === "corrupt") {
    title = "نتایج ذخیره‌شده نیازمند آزمون تازه‌اند";
    description = current.message;
  } else if (current.rows.length && !visible.rows.length) {
    title = "نتیجه‌ای با این فیلترها پیدا نشد";
    description = "نام سرور یا منبع دیگری جست‌وجو کنید یا فیلترها را پاک کنید.";
    byId("clear-filters").hidden = false;
  } else if (current.state === "ready") {
    title = current.summary.interrupted ? "تا زمان توقف، نتیجهٔ تأییدشده‌ای به دست نیامد" : "این آزمون نتیجهٔ تأییدشده‌ای نداشت";
    description = current.summary.interrupted ? "نتایج فقط شامل نامزدهای آزموده‌شده است؛ برای باقی موارد اسکن را ادامه دهید." : "وضعیت منابع و علت شکست‌ها را بررسی کنید و شبکه یا اسکن دیگری را امتحان کنید.";
  } else if (current.state === "legacy") {
    title = "نتایج قدیمی موجود نیست";
    description = "برای ساخت شواهد تازه و شمارنده‌های معتبر، آزمون جدید بگیرید.";
  }
  byId("empty-title").textContent = title;
  byId("empty-description").textContent = description;
}

function renderHealth(current) {
  const reports = current.source_reports;
  const reached = reports.filter(report => ["ok", "empty"].includes(report.status)).length;
  const sets = new Set(reports.map(row => row.content_id).filter(Boolean)).size;
  const publishers = new Set(reports.map(row => row.publisher_id).filter(Boolean)).size;
  byId("source-summary").textContent = reports.length ? `منابع دریافت‌شده: ${reached} / ${reports.length} · ${sets} محتوای متفاوت · ${publishers} حساب ناشر شناخته‌شده` : "منابع · بدون گزارش";
  byId("source-list").replaceChildren(...reports.map(report => {
    const checked = Number.isInteger(report.checked) ? ` · ${count(report.checked)} آزموده` : "";
    const qualified = Number.isInteger(report.qualified) ? ` · ${count(report.qualified)} واجد شرایط هنگام آزمون` : "";
    const cooldown = Number.isFinite(report.cooldown_until) && report.cooldown_until * 1000 > Date.now() ? ` · تلاش بعدی ${new Date(report.cooldown_until * 1000).toLocaleString()}` : "";
    return node("li", `${report.name} · ${report.status} · ${report.count} نامزد${checked}${qualified}${cooldown}${report.mirror_count > 1 ? ` · ${report.mirror_count} آینهٔ محتوایی` : ""}${report.error ? ` · ${report.error}` : ""}`, ["error", "cooldown"].includes(report.status) ? "source-error" : "");
  }));
  if (!reports.length) byId("source-list").append(node("li", "گزارش منابع پس از جمع‌آوری کامل نمایش داده می‌شود."));
  const failures = Object.entries(current.summary.failure_counts || {});
  byId("failure-summary").textContent = failures.length ? `علت شکست‌ها · ${failures.length} دسته` : "علت شکست‌ها · گزارشی نیست";
  byId("failure-list").replaceChildren(...failures.map(([reason, total]) => node("li", `${reason.replaceAll("_", " ")}: ${count(total)}`)));
  if (!failures.length) byId("failure-list").append(node("li", "علت شکستی برای این نتیجه ثبت نشده است."));
}

function render(current, force = false) {
  if (!current) {
    current = {state: "missing", message: "نتیجهٔ اسکن بارگذاری نشده است.", rows: [], summary: {}, source_reports: []};
  }
  const summary = current.summary;
  if (summary.provisional === true) current = {...current, rows: current.rows.map(row => ({...row, provisional: true}))};
  clearTimeout(expiryTimer);
  const expiry = Math.min(...current.rows.map(row => row.evidence_expires_at * 1000).filter(value => Number.isFinite(value) && value > Date.now()));
  if (Number.isFinite(expiry)) expiryTimer = setTimeout(() => render(snapshots.get(mode), true), Math.max(50, expiry - Date.now() + 20));
  renderActivity(current);
  const profile = mode === "web" ? "web" : summary.verification_profile || "transport";
  const strict = profile === "telegram_session" && current.rows.every(strictQualified) && [summary.strict_verified, summary.transport_reachable, summary.tested].every(Number.isInteger) && current.rows.length <= summary.strict_verified && summary.strict_verified <= summary.transport_reachable && summary.transport_reachable <= summary.tested && summary.verified === summary.strict_verified;
  const invalidStrict = profile === "telegram_session" && !strict;
  if (invalidStrict) current = {...current, state: "corrupt", rows: [], message: "شواهد آزمون دقیق معتبر نیست؛ دوباره آزمایش کنید."};
  byId("verification-status").hidden = mode === "web";
  byId("verification-status").textContent = invalidStrict ? "شواهد آزمون دقیق معتبر نیست؛ دوباره آزمایش کنید." : strict
    ? "تأیید هویت سرور تلگرام و پینگ رمز‌شده؛ نتیجهٔ مدیا در بخش جداگانه قرار می‌گیرد."
    : "شواهد اتصال اولیه؛ برای تأیید دقیق تلگرام دوباره آزمون بگیرید.";
  byId("verified-label").textContent = mode === "web" ? "تأییدشده" : "تأیید دقیق";
  byId("verified-description").textContent = mode === "web" ? "آزمون وب موفق" : "جلسه‌های واجد شرایط";
  const interrupted = summary.interrupted === true;
  byId("tested-description").textContent = interrupted ? "نامزدهای دارای نمونه" : "آزمون‌های کامل";
  byId("partial-note").hidden = !interrupted;
  byId("partial-note").textContent = `اسکن متوقف شد؛ نتایج موقت. ${count(summary.tested)} از ${count(summary.selected)} نامزد آزموده و ${count(summary.skipped)} مورد رد شده از این دور. رتبه‌ها فقط مربوط به موارد آزموده‌شده‌اند.`;
  if (interrupted && summary.provisional === true) byId("partial-note").textContent += " موقت: بازآزمایی پایدار نهایی کامل نشده است؛ اسکن ذخیره‌شده را ادامه دهید.";
  byId("scan-time-label").textContent = interrupted ? "زمان توقف آزمون" : "آخرین آزمون کامل";
  byId("results-title").textContent = mode !== "web" && !strict ? interrupted ? "شواهد موقت اتصال" : "شواهد اتصال؛ نیازمند آزمون دقیق" : interrupted ? "نتایج موقت آزموده‌شده" : "بهترین نتایج تأییدشده";
  if (summary.strategy === "staged" && strict) byId("results-title").textContent = summary.provisional ? "نتایج فعلی؛ آزمون نهایی هنوز کامل نیست" : "بهترین‌ها پس از بازآزمایی نهایی";
  for (const key of ["selected", "tested"]) byId(`count-${key}`).textContent = count(summary[key]);
  byId("count-verified").textContent = count(mode === "web" ? summary.verified : strict ? summary.strict_verified : 0);
  const timestamp = summary.completed_at ? new Date(summary.completed_at) : null;
  if (timestamp && !Number.isNaN(timestamp.getTime())) {
    byId("scan-time").textContent = timestamp.toLocaleString();
    byId("scan-time").dateTime = summary.completed_at;
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp.getTime()) / 60000));
    byId("freshness").textContent = minutes < 1 ? "تازه کامل شد" : minutes < 60 ? `${minutes} دقیقه پیش` : `${Math.floor(minutes / 60)} ساعت پیش`;
    byId("freshness").className = minutes >= 60 ? "pill warning" : "pill neutral";
    byId("scan-meta").textContent = `درخواست: ${summary.requested_count == null ? "ALL" : count(summary.requested_count)} · جمع‌آوری: ${count(summary.collected)} · واجد شرایط: ${count(summary.eligible)} · آزموده‌نشده: ${count(summary.skipped ?? (summary.selected - summary.tested))}${mode !== "web" ? ` · دسترسی اولیه: ${count(summary.transport_reachable)}` : ""}`;
  } else {
    byId("scan-time").textContent = current.state === "legacy" ? "زمان نامشخص · نتایج قدیمی" : "اسکن کاملی موجود نیست";
    byId("scan-time").removeAttribute("datetime");
    byId("freshness").textContent = current.state === "corrupt" ? "نتیجهٔ نامعتبر" : current.state === "legacy" ? "نتیجهٔ قدیمی" : "در انتظار نتیجه";
    byId("freshness").className = current.state === "corrupt" ? "pill warning" : "pill neutral";
    byId("scan-meta").textContent = current.message || "آزمون را شروع کنید تا نتایج اینجا نمایش داده شوند.";
  }
  const expiredCount = current.rows.filter(row => evidenceState(row) === "expired").length;
  const unknownCount = current.rows.filter(row => evidenceState(row) === "missing").length;
  if (expiredCount || unknownCount) {
    byId("freshness").textContent = expiredCount ? `${expiredCount} نتیجه منقضی؛ بازآزمایی لازم` : "زمان اعتبار نامشخص؛ بازآزمایی لازم";
    byId("freshness").className = "pill warning";
  }
  // v3 scans have unique run IDs. Legacy/custom snapshots need their contents
  // compared because two completed scans can share a timestamp and row count.
  const revision = typeof summary.run_id === "string" && summary.run_id ? summary.run_id : JSON.stringify({summary, rows: current.rows, source_reports: current.source_reports});
  const identity = `${mode}:${current.state}:${revision}`;
  if (force || identity !== renderedIdentity || current.rows.some(row => !evidenceActionable(row))) {
    renderResults(current);
    renderHealth(current);
    renderedIdentity = identity;
    refreshLibrary();
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
  const requestNetwork = network;
  refreshControl();
  const ticket = ++generation;
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 10000);
  byId("refresh").disabled = true;
  byId("refresh").setAttribute("aria-busy", "true");
  status("در حال تازه‌سازی نتایج…");
  try {
    const response = await fetch(`/api/snapshot?mode=${requestMode}${networkScoped ? `&network=${encodeURIComponent(requestNetwork)}` : ""}`, {cache: "no-store", signal: controller.signal});
    if (!response.ok) throw new Error(`Dashboard returned HTTP ${response.status}`);
    const current = await response.json();
    if (ticket !== generation || requestMode !== mode || requestNetwork !== network) return;
    if (current.mode !== requestMode || !["ready", "missing", "legacy", "corrupt"].includes(current.state) || !Array.isArray(current.rows) || !current.summary || !Array.isArray(current.source_reports)) throw new Error("Invalid dashboard response");
    if (!networkScoped && typeof current.network === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(current.network)) {
      network = current.network;
      networkScoped = true;
      byId("control-network").value = network;
    }
    snapshots.set(requestMode, current);
    connected = true;
    render(current, force);
    byId("connection-state").textContent = "داشبورد محلی";
    byId("connection-state").className = "pill";
    const progress = validProgress(current);
    status(progress?.active ? `${stages[progress.stage]} در ترمینال در حال اجرا است؛ زمان نتایج ذخیره‌شده مستقل است.` : current.state === "ready" ? current.summary.interrupted ? `نتیجهٔ موقت: ${count(current.summary.tested)} مورد آزموده و ${count(current.summary.skipped)} مورد آزموده‌نشده. وضعیت پروکسی می‌تواند تغییر کند.` : "نتیجهٔ یک آزمون کامل نمایش داده می‌شود؛ وضعیت پروکسی می‌تواند بعداً تغییر کند." : current.message, current.state === "corrupt");
  } catch (error) {
    if (ticket !== generation || (!timedOut && error.name === "AbortError")) return;
    connected = false;
    byId("connection-state").textContent = "نشست در دسترس نیست";
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
  for (const value of visibleModes) {
    const tab = byId(`tab-${value}`);
    tab.setAttribute("aria-selected", String(value === (mode === "telegram" ? "mtproto" : mode)));
    tab.tabIndex = value === (mode === "telegram" ? "mtproto" : mode) ? 0 : -1;
  }
  if (focus) byId(`tab-${mode === "telegram" ? "mtproto" : mode}`).focus();
  byId("proxy-panel").setAttribute("aria-labelledby", `tab-${mode === "telegram" ? "mtproto" : mode}`);
  byId("link-format-field").hidden = !["mtproto", "telegram"].includes(mode);
  byId("source-protocol-field").hidden = mode !== "web";
  if (mode === "telegram") linkFormat = "https";
  byId("link-format").value = linkFormat;
  byId("mode-label").textContent = mode === "mtproto" ? "پروکسی تلگرام" : mode === "telegram" ? "لینک اشتراک MTProto" : mode === "webproxy" ? "TELEGRAM WEB · آزمایشی" : "پروکسی HTTP / SOCKS";
  byId("mode-title").textContent = mode === "mtproto" ? "بهترین اتصال‌های MTProto شما" : mode === "telegram" ? "بهترین اتصال‌های MTProto شما" : mode === "webproxy" ? "بهترین پروکسی‌های Telegram WEB" : "بهترین پروکسی‌های HTTP / SOCKS";
  byId("mode-description").textContent = mode === "mtproto" ? "تأیید هویت سرور MTProto و پینگ‌های رمز‌شدهٔ تکراری؛ رتبه بر پایهٔ پاسخ و اتصال مجدد." : mode === "telegram" ? "همان آزمون دقیق MTProto با لینک اشتراک HTTPS و مشخصات اصلی اتصال." : mode === "webproxy" ? "حامل WEB از HTTPS و WSS با گواهی معتبر استفاده می‌کند. پیشنهاد اتصال نیازمند تأیید هویت تلگرام و پینگ رمز‌شده از همین مسیر است." : "آزمون پروکسی‌های HTTP، HTTPS و SOCKS با درخواست HTTPS دارای گواهی معتبر.";
  byId("verification-note").textContent = mode !== "web" ? "پینگ تلگرام زمان پاسخ رمز‌شده است؛ آزمون مدیا جداگانه است. نمونه‌های کوتاه، تضمین دسترسی بلندمدت نیستند." : "آزمون، دسترسی به مقصد HTTPS تنظیم‌شده را در همان لحظه بررسی می‌کند.";
  byId("telegram-rtt-sort").hidden = mode === "web";
  byId("probe-duration-sort").textContent = mode === "web" ? "کمترین تأخیر" : "زمان آزمون (تشخیصی)";
  byId("consistency-sort").textContent = mode === "web" ? "باثبات‌ترین" : "ثبات آزمون (تشخیصی)";
  byId("scan-command").textContent = `python main.py best10 --mode ${mode} --count ALL`;
  byId("protocol-field").hidden = mode !== "web";
  byId("search").value = "";
  byId("protocol").value = "all";
  byId("sortBy").value = "score";
  history.replaceState(null, "", `?mode=${mode}${networkScoped ? `&network=${encodeURIComponent(network)}` : ""}`);
  renderControl();
  render(snapshots.get(mode), true);
  refreshLibrary(true);
  refresh(true);
}

for (const value of visibleModes) {
  const tab = byId(`tab-${value}`);
  tab.addEventListener("click", () => { if (value !== mode) selectMode(value); });
  tab.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = (visibleModes.indexOf(mode === "telegram" ? "mtproto" : mode) + (event.key === "ArrowLeft" ? 1 : -1) + visibleModes.length) % visibleModes.length;
      selectMode(event.key === "Home" ? visibleModes[0] : event.key === "End" ? visibleModes.at(-1) : visibleModes[next], true);
    }
  });
}
byId("refresh").addEventListener("click", () => refresh(true));
byId("pause").addEventListener("click", () => {
  paused = !paused;
  byId("pause").setAttribute("aria-pressed", String(paused));
  byId("pause").textContent = paused ? "ادامهٔ به‌روزرسانی" : "توقف به‌روزرسانی";
  clearTimeout(pollTimer);
  status(paused ? "به‌روزرسانی خودکار متوقف شد؛ تازه‌سازی دستی فعال است." : connected ? "به‌روزرسانی خودکار ادامه یافت." : "در حال بررسی داشبورد محلی…");
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
byId("copy-resume").addEventListener("click", event => copyText(terminalCommand(snapshots.get(mode), true), event.currentTarget));
byId("copy-recheck").addEventListener("click", event => copyText(terminalCommand(snapshots.get(mode), false, true), event.currentTarget));
byId("control-scope").addEventListener("change", renderControl);
for (const action of ["start", "stop", "resume", "recheck"]) byId(`control-${action}`).addEventListener("click", () => sendControl(action));
byId("control-network").addEventListener("change", () => {
  try {
    const selected = controlPayload("recheck").network;
    if (selected !== network) {
      network = selected;
      networkScoped = true;
      networkChangedByUser = true;
      snapshots.clear();
      history.replaceState(null, "", `?mode=${mode}&network=${encodeURIComponent(network)}`);
      render(snapshots.get(mode), true);
      refresh(true);
    }
    renderControl();
  } catch (error) { controlFeedback(error.message, true); }
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(pollTimer);
  if (document.hidden) { if (activeRequest) activeRequest.abort(); }
  else if (!paused) refresh(false);
});

function evidenceState(proxy) {
  const expiry = proxy.evidence_expires_at;
  if (!Number.isFinite(expiry)) return "missing";
  return expiry * 1000 > Date.now() ? proxy.provisional === true ? "provisional" : "fresh" : "expired";
}
function evidenceActionable(proxy) { return ["fresh", "provisional"].includes(evidenceState(proxy)); }
function shareConnection(connection) {
  if (!["mtproto", "telegram"].includes(mode)) return connection;
  const query = new URL(connection).search;
  return (linkFormat === "https" ? "https://t.me/proxy" : "tg://proxy") + query;
}
async function libraryRequest(payload) {
  if (!controlToken) throw new Error("کنترل محلی در دسترس نیست؛ ترمینال را باز نگه دارید.");
  const pending = new AbortController();
  const timeout = setTimeout(() => pending.abort(), 15000);
  try {
    const response = await fetch("/api/library", {method: "POST", credentials: "same-origin", cache: "no-store", signal: pending.signal, headers: {"Content-Type": "application/json", "X-Revmamad-Token": controlToken}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "عملیات انجام نشد.");
    return result;
  } finally { clearTimeout(timeout); }
}
function libraryFeedback(message, error = false) {
  byId("library-feedback").textContent = message;
  byId("library-feedback").classList.toggle("error", error);
}
async function libraryAction(payload) {
  try {
    await libraryRequest(payload);
    libraryRevision += 1;
    libraryFeedback(payload.action === "favourite_recheck" ? "بازآزمایی تازه آغاز شد؛ نتیجهٔ ذخیره‌شده به‌تنهایی تأیید اتصال نیست." : "تغییرات شخصی ذخیره شد.");
    await refreshLibrary(true);
    await refreshControl();
    refresh(true);
  } catch (error) { libraryFeedback(error.name === "AbortError" ? "پاسخ دیر رسید؛ وضعیت را تازه‌سازی کنید." : error.message, true); }
}
let libraryPending = false;
let libraryRevision = 0;
let libraryRefreshQueued = false;
async function refreshLibrary(force = false) {
  if (!controlToken) return;
  if (libraryPending) { if (force) libraryRefreshQueued = true; return; }
  libraryPending = true;
  const currentMode = mode, currentNetwork = network, revision = libraryRevision;
  try {
    const result = await libraryRequest({action: "list", mode, network});
    if (mode !== currentMode || network !== currentNetwork || revision !== libraryRevision) return;
    byId("library-sources").replaceChildren(...result.sources.map(source => {
      const item = node("li", "", "library-row");
      item.append(node("span", `${source.name} · ${source.personal ? "شخصی" : "پیش‌فرض"} · ${source.enabled ? "فعال" : "غیرفعال"}`));
      const toggle = node("button", source.enabled ? "غیرفعال کردن" : "فعال کردن", "button quiet");
      toggle.type = "button";
      toggle.addEventListener("click", () => libraryAction({action: "source_toggle", id: source.id, enabled: !source.enabled}));
      item.append(toggle);
      return item;
    }));
    byId("library-favourites").replaceChildren(...result.favourites.map(favourite => {
      const item = node("li", "", "library-row");
      item.append(node("span", `${favourite.server}:${favourite.port} · نیازمند بازآزمایی`));
      const remove = node("button", "حذف علاقه‌مندی", "button quiet");
      remove.type = "button";
      remove.addEventListener("click", () => libraryAction({action: "favourite_remove", id: favourite.id}));
      item.append(remove);
      return item;
    }));
    if (!result.favourites.length) byId("library-favourites").append(node("li", "در این گروه و شبکه هنوز علاقه‌مندی ذخیره نشده است."));
    byId("favourites-recheck").disabled = !result.favourites.length;
    const media = await libraryRequest({action: "media_report", mode, network});
    if (mode === currentMode && network === currentNetwork && revision === libraryRevision) renderMedia(media.report);
  } catch (error) { libraryFeedback(error.message, true); }
  finally { libraryPending = false; if (libraryRefreshQueued) { libraryRefreshQueued = false; await refreshLibrary(); } }
}
function renderMedia(report) {
  const container = byId("media-report");
  container.replaceChildren();
  if (!report || report.schema_version !== 1 || report.experimental !== true || report.network_profile !== network || !Array.isArray(report.results)) {
    container.append(node("p", "برای این گروه و شبکه گزارش مدیای معتبر موجود نیست. آزمون اختیاری را فقط از ترمینال اجرا کنید."));
    return;
  }
  const number = value => Number.isFinite(value) && value >= 0 ? value.toFixed(2) : "—";
  const status = {complete: "کامل", completed: "کامل", partial: "ناقص", stopped: "متوقف", cancelled: "متوقف", failed: "ناموفق", budget_exceeded: "سقف مصرف", budget_limit: "سقف مصرف", timeout: "پایان زمان", time_limit: "پایان زمان", expired_evidence: "اعتبار آزمون پروتکل منقضی", evidence_expired: "اعتبار آزمون پروتکل منقضی", cdn_unsupported: "مسیر CDN پشتیبانی‌نشده", file_unavailable: "فایل در دسترس نیست", account_limited: "محدودیت حساب", file_changed: "فایل تغییر کرده", authentication_failed: "ورود ناموفق", network_failed: "خطای شبکه", protocol_failed: "خطای پروتکل", short_sample: "نمونهٔ کوتاه", not_tested: "آزموده‌نشده", internal_error: "خطای داخلی", ok: "موفق", passed: "موفق"};
  const label = value => status[value] || "وضعیت ثبت‌شده؛ گزارش ترمینال را ببینید";
  container.append(node("p", `آزمایشی · ${label(report.status)} · ${report.network_profile} · ${new Date(report.created_at * 1000).toLocaleString()}`, "measurement-note"));
  container.append(node("p", `دریافت فایل: ${number(report.downloaded_bytes / 1048576)} MiB · ترافیک اندازه‌گیری‌شده: ${number(report.observed_bytes / 1048576)} از ${number(report.budget_bytes / 1048576)} MiB · زمان: ${number(report.elapsed_seconds)} ثانیه`, "measurement-note"));
  const logout = {success: "خروج تأیید شد", logged_out: "خروج تأیید شد", confirmed: "خروج تأیید شد", failed: "خروج تأیید نشد؛ نشست‌ها را در تلگرام بررسی کنید", timeout: "خروج تأیید نشد؛ نشست‌ها را در تلگرام بررسی کنید", not_authorized: "حسابی وارد نشده", not_logged_in: "حسابی وارد نشده", not_confirmed: "خروج تأیید نشد؛ نشست‌ها را در تلگرام بررسی کنید", not_needed: "خروج لازم نبود"};
  container.append(node("p", `وضعیت خروج: ${logout[report.logout_status] || "تأیید خروج موجود نیست؛ گزارش ترمینال را بررسی کنید"}`, "measurement-note"));
  const rankings = Array.isArray(report.ranked_candidate_ids) ? report.ranked_candidate_ids : [];
  const ordered = [...report.results].sort((a, b) => {
    const first = rankings.indexOf(a.candidate_id), second = rankings.indexOf(b.candidate_id);
    return (first < 0 ? 999 : first) - (second < 0 ? 999 : second);
  });
  for (const candidate of ordered.slice(0, 5)) {
    if (!/^[a-f0-9]{64}$/.test(candidate.candidate_id) || !Array.isArray(candidate.rounds)) continue;
    const item = node("div", "", "result-card");
    const rank = rankings.indexOf(candidate.candidate_id);
    item.append(node("strong", `${rank < 0 ? "بدون رتبه" : `رتبهٔ مدیا ${rank + 1}`} · ${candidate.candidate_id.slice(0, 16)}`));
    const current = snapshots.get(mode)?.rows.find(row => row.candidate_id === candidate.candidate_id);
    if (current) item.append(node("p", `مرتبط با نتیجهٔ ${current.rank}: ${current.server}:${current.port}`, "measurement-note"));
    item.append(node("p", `${label(candidate.status)} · ${count(candidate.completed_rounds)} / 2 دور کامل · سرعت میانه ${number(candidate.median_mbps)} Mbps · اولین بایت ${number(candidate.median_ttfb_ms)} ms · توقف ${count(candidate.stalls)}`, "measurement-note"));
    const fresh = Number.isFinite(candidate.proof_expires_at) && candidate.proof_expires_at * 1000 > Date.now();
    item.append(node("p", fresh ? "این گزارش فقط عملکرد همین فایل و نمونهٔ آزموده‌شده را نشان می‌دهد." : "اعتبار آزمون پروتکل تمام شده؛ این رتبهٔ تاریخی مدیا، تأیید اتصال فعلی نیست.", "measurement-note"));
    for (const [index, round] of candidate.rounds.slice(0, 2).entries()) {
      item.append(node("p", `دور ${index + 1}: ${label(round.status)} · ${number(round.bytes / 1048576)} MiB · ${number(round.mbps)} Mbps · ${number(round.elapsed_seconds)} ثانیه${round.short_sample ? " · نمونهٔ کوتاه؛ اطمینان محدود" : ""}`, "measurement-note"));
    }
    container.append(item);
  }
}
function downloadDocument(document, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(document, null, 2)], {type: "application/json;charset=utf-8"}));
  const link = node("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
byId("source-add").addEventListener("click", () => libraryAction({action: "source_add", mode, name: byId("source-name").value.trim() || "منبع شخصی", url: byId("source-url").value.trim(), type: byId("source-parser").value, protocol: mode === "web" ? byId("source-protocol").value : "http"}));
byId("source-manual").addEventListener("click", () => libraryAction({action: "source_manual", mode, name: byId("source-name").value.trim() || "لینک‌های شخصی", text: byId("manual-links").value}));
byId("favourites-recheck").addEventListener("click", () => libraryAction({action: "favourite_recheck", mode, network}));
byId("sources-export").addEventListener("click", async () => {
  try { const result = await libraryRequest({action: "source_export"}); downloadDocument(result.document, "revmamad-personal-sources.json"); libraryFeedback("فایل منابع شخصی دریافت شد؛ لینک‌های خصوصی خود را عمومی نکنید."); }
  catch (error) { libraryFeedback(error.message, true); }
});
byId("doctor-download").addEventListener("click", async () => {
  try { const result = await libraryRequest({action: "doctor"}); downloadDocument(result.document, "revmamad-diagnostics.json"); libraryFeedback("گزارش عیب‌یابی بدون اطلاعات حساب دریافت شد."); }
  catch (error) { libraryFeedback(error.message, true); }
});
byId("sources-import").addEventListener("change", async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > 3 * 1024 * 1024) throw new Error("فایل بیش از ۳ مگابایت است؛ از فرمان sources در ترمینال استفاده کنید.");
    await libraryAction({action: "source_import", document: JSON.parse(await file.text())});
  } catch (error) { libraryFeedback(error.message, true); }
  finally { event.target.value = ""; }
});
byId("link-format").addEventListener("change", () => { linkFormat = byId("link-format").value === "https" ? "https" : "tg"; render(snapshots.get(mode), true); });
let theme = "dark";
try { theme = localStorage.getItem("revmamad-theme") === "light" ? "light" : "dark"; } catch {}
function applyTheme() {
  document.documentElement?.setAttribute("data-theme", theme);
  byId("theme-toggle").textContent = theme === "dark" ? "حالت روشن" : "حالت تیره";
  byId("theme-toggle").setAttribute("aria-pressed", String(theme === "light"));
}
byId("theme-toggle").addEventListener("click", () => { theme = theme === "dark" ? "light" : "dark"; try { localStorage.setItem("revmamad-theme", theme); } catch {} applyTheme(); });
applyTheme();
selectMode(mode);
