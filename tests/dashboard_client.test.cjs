// Execute the shipped client with a small DOM substitute; no browser or npm packages.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor() {
    this.value = "";
    this.classList = {toggle() {}};
    this.children = [];
    this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() {}
  select() {}
}

function client() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  element("protocol").value = "all";
  element("sortBy").value = "score";
  element("limit").value = "10";
  const context = vm.createContext({
    URL, URLSearchParams, AbortController, Map, console, atob,
    location: {search: ""}, history: {replaceState() {}}, navigator: {},
    setTimeout() { return 0; }, clearTimeout() {},
    fetch() { return new Promise(() => {}); },
    document: {hidden: false, getElementById: element,
      createElement() { return new Element(); }, addEventListener() {}},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../dashboard/dashboard.js"), "utf8"), context);
  return {context, element};
}

function strongRow(extra = {}) {
  return {
    server: "proxy.example", port: 443, source: "fixture", rank: 1, evidence_expires_at: Date.now() / 1000 + 1800,
    secret: "00112233445566778899aabbccddeeff",
    tg_link: "tg://proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff",
    verification_profile: "telegram_session", strict_eligible: true,
    attempts: 3, successes: 3, strict_successes: 3, success_rate: 1,
    dc_ids_verified: [2, 4], ping_count: 9,
    telegram_median_ms: 100, telegram_p95_ms: 120, telegram_jitter_ms: 5,
    reliability_lower_bound: .4385, avg_latency_ms: 2000, stability: .9, score: .8,
    strict_policy: {min_successes: 3, min_success_rate: .8, required_dc_ids: [2,4], min_ping_count: 3, max_p95_ms: 1500, max_jitter_ms: 500},
    ...extra,
  };
}

function allText(element) {
  return [element.textContent || "", ...element.children.map(allText)].join(" ");
}

function webRow(path = "base/path", dd = false) {
  const inner = (dd ? "dd" : "") + "00112233445566778899aabbccddeeff";
  const server = "relay.example" + (path ? "/" + path : "");
  const encoded = path ? Buffer.concat([Buffer.from([0x70]), Buffer.from(inner, "hex")]).toString("base64url") : inner;
  const query = new URLSearchParams({server, secret: encoded});
  return strongRow({kind: "webproxy", protocol: "webproxy", server, secret: inner, tg_link: "tg://webproxy?" + query, web_link: "https://t.me/webproxy?" + query});
}

test("WEB relay actions match path and inner secret while keeping all strict gates", () => {
  const {context} = client();
  vm.runInContext("mode = 'webproxy'", context);
  for (const path of ["", "base/path"]) for (const dd of [false, true]) {
    context.row = webRow(path, dd);
    assert.equal(vm.runInContext("safeConnection(row)", context), context.row.tg_link);
  }
  for (const extra of [
    {strict_eligible: false}, {ping_count: 1}, {dc_ids_verified: [2]}, {protocol: "mtproto"},
    {tg_link: "tg://proxy?server=relay.example&port=443&secret=00112233445566778899aabbccddeeff"},
    {web_link: webRow("other").web_link}, {tg_link: webRow("other").tg_link},
    {web_link: webRow().web_link + "&port=443"},
    {web_link: webRow().web_link.replace("t.me", "evil.example")},
    {web_link: "https://t.me/webproxy?server=relay.example%2Fbase%2Fpath&secret=00112233445566778899aabbccddeeff"},
  ]) {
    context.row = {...webRow(), ...extra};
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
});

test("WEB direct app action and HTTPS copy stay distinct and show compatibility limitation", () => {
  const {context} = client();
  vm.runInContext("mode = 'webproxy'", context);
  context.row = webRow();
  const card = vm.runInContext("resultCard(row)", context);
  const text = allText(card);
  assert.match(text, /اتصال Telegram WEB/);
  assert.match(text, /کپی لینک HTTPS وب/);
  assert.match(text, /تلگرام سازگار با WEB/);
  assert.match(text, /لینک HTTPS.*درست باز نشود/);
  assert.doesNotMatch(text, /relay.example\/base\/path:443/);
});

test("control payloads separate ALL count time and explicit resume target", () => {
  const {context, element} = client();
  element("control-network").value = "MOBILE";
  element("control-scope").value = "all";
  assert.deepEqual(JSON.parse(vm.runInContext("JSON.stringify(controlPayload('start'))", context)), {action: "start", mode: "mtproto", network: "mobile"});
  element("control-scope").value = "count";
  element("control-count").value = "25";
  assert.equal(vm.runInContext("controlPayload('start').count", context), 25);
  element("control-scope").value = "10";
  assert.equal(vm.runInContext("controlPayload('start').minutes", context), 10);
  element("control-resume-kind").value = "recheck";
  assert.equal(vm.runInContext("controlPayload('resume').command", context), "recheck");
  assert.equal(vm.runInContext("controlPayload('resume').minutes", context), undefined);
});

test("invalid control fields never become shell commands or scan payloads", () => {
  const {context, element} = client();
  element("control-scope").value = "all";
  for (const network of ["../mobile", "CON", "wifi;echo", "", "a".repeat(33)]) {
    element("control-network").value = network;
    assert.throws(() => vm.runInContext("controlPayload('start')", context), /نام شبکه/);
  }
  element("control-network").value = "wifi";
  element("control-scope").value = "count";
  for (const count of ["0", "1000001", "1.5", "25;echo", ""]) {
    element("control-count").value = count;
    assert.throws(() => vm.runInContext("controlPayload('start')", context), /تعداد باید/);
  }
});

test("single active job disables new scans and only its matching scope can stop", () => {
  const {context, element} = client();
  vm.runInContext("controlToken = 'local-token'; controlJob = {mode:'webproxy',network:'mobile',state:'running'}; mode='mtproto'; network='mobile'; renderControl()", context);
  assert.equal(element("control-start").disabled, true);
  assert.equal(element("control-resume").disabled, true);
  assert.equal(element("control-stop").disabled, true);
  vm.runInContext("mode='webproxy'; renderControl()", context);
  assert.equal(element("control-stop").disabled, false);
  vm.runInContext("controlJob.state='stopping'; renderControl()", context);
  assert.equal(element("control-stop").disabled, true);
});

test("plain quality reasons describe observed reconnect and latency limits", () => {
  const {context} = client();
  context.row = strongRow({attempts: 8, strict_successes: 7});
  assert.match(vm.runInContext("qualityReason(row, true)", context), /شکست اتصال مجدد/);
  context.row = strongRow({telegram_p95_ms: 1000});
  assert.match(vm.runInContext("qualityReason(row, true)", context), /پاسخ تلگرام کندتر/);
  context.row = strongRow();
  assert.match(vm.runInContext("qualityReason(row, true)", context), /نمونهٔ کوتاه/);
});

test("strict cards show encrypted Telegram RTT and counts rather than download speed", () => {
  const {context} = client();
  context.row = strongRow();
  const card = vm.runInContext("resultCard(row)", context);
  const text = allText(card);
  assert.match(text, /Telegram RTT p95.*120 ms/);
  assert.match(text, /3\s*\/\s*3/);
  assert.match(text, /مدت آزمون.*2000 ms/);
  assert.match(text, /9 پاسخ پینگ معتبر/);
  assert.doesNotMatch(text, /download|Mbps|MB\/s|Speed:/i);
});

test("fast single-sample and malformed proof never get usable Telegram actions", () => {
  const {context} = client();
  for (const extra of [
    {attempts: 1, successes: 1, strict_successes: 1, ping_count: 3, avg_latency_ms: 1},
    {verification_profile: "transport"}, {strict_policy: null}, {dc_ids_verified: [2]},
    {ping_count: 8}, {telegram_p95_ms: 1501}, {strict_eligible: "true"},
  ]) {
    context.row = strongRow(extra);
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
  context.row = strongRow();
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.tg_link);
});

test("legacy snapshot is transport evidence and requests a strict rescan", () => {
  const {context, element} = client();
  context.current = {state: "ready", summary: {verified: 1}, source_reports: [], rows: [strongRow({verification_profile: undefined, strict_policy: undefined, attempts: 1, successes: 1})]};
  vm.runInContext("render(current, true)", context);
  assert.match(element("verification-status").textContent, /شواهد اتصال.*دوباره آزمون/);
  const text = allText(element("results"));
  assert.match(text, /نمونه کافی نیست/);
  assert.doesNotMatch(text, /Telegram session verified/);
  assert.equal(element("count-verified").textContent, "0");
});

test("Telegram latency sorting uses p95 instead of connection setup duration", () => {
  const {context, element} = client();
  context.current = {rows: [strongRow({rank: 1, avg_latency_ms: 1, telegram_p95_ms: 400}), strongRow({rank: 2, avg_latency_ms: 2000, telegram_p95_ms: 120})]};
  element("sortBy").value = "telegram_p95_ms";
  assert.deepEqual(Array.from(vm.runInContext("filteredRows(current).rows.map(row => row.rank)", context)), [2,1]);
});

test("a strict snapshot label cannot promote missing proof into counters or badges", () => {
  const {context, element} = client();
  context.current = {state: "ready", source_reports: [],
    summary: {verification_profile: "telegram_session", tested: 1, verified: 1, strict_verified: 1, transport_reachable: 1},
    rows: [strongRow({strict_policy: undefined})]};
  vm.runInContext("render(current, true)", context);
  assert.equal(element("count-verified").textContent, "0");
  assert.match(element("verification-status").textContent, /آزمون دقیق معتبر نیست/);
  assert.equal(element("results").children.length, 0);
});

test("web copy actions accept default ports, SOCKS and IPv6", () => {
  const {context} = client();
  vm.runInContext("mode = 'web'", context);
  for (const row of [
    {server: "proxy.example", port: 80, protocol: "http", uri: "http://proxy.example:80"},
    {server: "proxy.example", port: 443, protocol: "https", uri: "https://proxy.example:443"},
    {server: "proxy.example", port: 1080, protocol: "socks5", uri: "socks5://proxy.example:1080"},
    {server: "2001:db8::1", port: 8080, protocol: "http", uri: "http://[2001:db8::1]:8080"},
  ]) {
    context.row = {...row, evidence_expires_at: Date.now() / 1000 + 1800};
    assert.equal(vm.runInContext("safeConnection(row)", context), row.uri);
  }
});

test("connection actions reject credentials, wrong endpoints and injected schemes", () => {
  const {context} = client();
  vm.runInContext("mode = 'web'", context);
  for (const uri of ["javascript:alert(1)", "http://other.example:80", "http://user:pass@proxy.example:80", "http://proxy.example:80/#unsafe"]) {
    context.row = {server: "proxy.example", port: 80, protocol: "http", uri};
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
  vm.runInContext("mode = 'mtproto'", context);
  context.row = strongRow();
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.tg_link);
  context.row.tg_link += "&port=80";
  assert.equal(vm.runInContext("safeConnection(row)", context), null);
});

test("default top ten keeps the published ranking and filters before limiting", () => {
  const {context, element} = client();
  vm.runInContext("mode = 'web'", context);
  context.snapshot = {rows: Array.from({length: 12}, (_, index) => ({
    rank: index + 1, server: `proxy-${index}.example`, source: index % 2 ? "other" : "feed",
    protocol: index % 2 ? "socks5" : "http", score: 1 - index / 100,
    avg_latency_ms: index === 11 ? null : 100 - index,
  }))};
  assert.equal(vm.runInContext("filteredRows(snapshot).rows.length", context), 10);
  assert.deepEqual(Array.from(vm.runInContext("filteredRows(snapshot).rows.map(row => row.rank)", context)), [1,2,3,4,5,6,7,8,9,10]);
  element("protocol").value = "socks5";
  element("search").value = "OTHER";
  assert.equal(vm.runInContext("filteredRows(snapshot).total", context), 6);
  element("sortBy").value = "avg_latency_ms";
  assert.equal(vm.runInContext("filteredRows(snapshot).rows.at(-1).rank", context), 12);
});

test("refresh replaces same-count, same-second snapshots and source health", () => {
  const {context, element} = client();
  context.current = {state: "ready", summary: {completed_at: "2026-10-08T12:00:00+00:00"},
    rows: [{rank: 1, server: "first.example", port: 443, score: .8, source: "feed"}],
    source_reports: [{name: "feed", status: "ok", count: 1}]};
  vm.runInContext("render(current)", context);
  context.current = {...context.current,
    rows: [{rank: 1, server: "second.example", port: 443, score: .8, source: "feed"}],
    source_reports: [{name: "feed", status: "ok", count: 9}]};
  vm.runInContext("render(current)", context);
  assert.equal(element("results").children[0].children[0].children[1].children[0].textContent, "second.example:443");
  assert.equal(element("source-list").children[0].textContent, "feed · ok · 9 نامزد");
  context.current.summary.run_id = "one";
  vm.runInContext("render(current)", context);
  context.current.summary.run_id = "two";
  vm.runInContext("render(current)", context);
  assert.match(vm.runInContext("renderedIdentity", context), /two/);
});

test("Telegram Web Link uses matched HTTPS share links and never HTTP proxy URIs", () => {
  const {context, element} = client();
  vm.runInContext("selectMode('telegram')", context);
  assert.equal(vm.runInContext("mode", context), "telegram");
  assert.match(element("mode-title").textContent, /MTProto/);
  assert.equal(element("protocol-field").hidden, true);
  context.row = strongRow({server: "proxy.example", port: 443, secret: "00112233445566778899aabbccddeeff",
    web_link: "https://t.me/proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff",
    uri: "http://proxy.example:443"});
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.web_link);
  for (const prefix of ["http://t.me/proxy", "https://t.me.evil.example/proxy", "https://t.me:8443/proxy", "https://user@t.me/proxy", "https://t.me/other"]) {
    context.row.web_link = prefix + "?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff";
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
});

test("three RTL keyboard tabs cycle and legacy share format remains supported", () => {
  const {context, element} = client();
  const arrow = {key: "ArrowRight", preventDefault() {}};
  for (const expected of ["web", "webproxy", "mtproto"]) {
    const current = vm.runInContext("mode", context);
    element(`tab-${current}`).listeners.keydown(arrow);
    assert.equal(vm.runInContext("mode", context), expected);
  }
  element("tab-mtproto").listeners.keydown({key: "End", preventDefault() {}});
  assert.equal(vm.runInContext("mode", context), "web");
  assert.match(element("mode-title").textContent, /HTTP.*SOCKS/);
  assert.equal(element("protocol-field").hidden, false);
});

test("interrupted scans display partial results and skipped candidates accurately", () => {
  const {context, element} = client();
  context.current = {state: "ready", summary: {completed_at: "2026-10-08T12:00:00+00:00",
    interrupted: true, selected: 8, tested: 3, skipped: 5, verified: 1, eligible: 1},
    rows: [{rank: 1, server: "partial.example", port: 443, source: "feed", score: .8}], source_reports: []};
  vm.runInContext("render(current, true)", context);
  assert.equal(element("partial-note").hidden, false);
  assert.match(element("partial-note").textContent, /اسکن متوقف شد.*موقت/);
  assert.match(element("scan-meta").textContent, /آزموده‌نشده: 5/);
  assert.match(element("scan-time-label").textContent, /توقف/);
  assert.equal(element("count-tested").textContent, "3");
  assert.equal(element("tested-description").textContent, "نامزدهای دارای نمونه");
  assert.match(element("results-title").textContent, /موقت/);
});

test("Telegram share links require a valid row secret and matching decoded identity", () => {
  const {context} = client();
  vm.runInContext("selectMode('telegram')", context);
  context.row = strongRow({web_link: "https://t.me/proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff"});
  for (const secret of [undefined, null, false, 1, "", "abcdefghijklmnop"]) {
    context.row.secret = secret;
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
  context.row.secret = Buffer.from("00112233445566778899aabbccddeeff", "hex").toString("base64url");
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.web_link);
  context.row.secret = "dd00112233445566778899aabbccddeeff";
  assert.equal(vm.runInContext("safeConnection(row)", context), null);
  const key = "fa".repeat(16);
  for (const hex of [key, "dd" + key, "ee" + key + Buffer.from("example.org").toString("hex")]) {
    context.row.secret = Buffer.from(hex, "hex").toString("base64url");
    context.row.web_link = `https://t.me/proxy?server=proxy.example&port=443&secret=${hex}`;
    assert.equal(vm.runInContext("safeConnection(row)", context), context.row.web_link);
  }
});

function liveProgress(extra = {}) {
  return {version: 1, mode: "mtproto", network: "mobile", command: "best10", run_id: "live-1",
    updated_at: new Date().toISOString(), stage: "finalists", completed: 2, total: 20,
    qualified: 1, remaining_seconds: 90, active: true, resumable: true, ...extra};
}

test("live stage updates even while the saved result run ID stays the same", () => {
  const {context, element} = client();
  context.current = {state: "missing", rows: [], summary: {run_id: "same"}, source_reports: [], network: "mobile", progress: liveProgress()};
  vm.runInContext("render(current)", context);
  assert.match(element("progress-label").textContent, /بازآزمایی پایدار/);
  assert.match(element("progress-meta").textContent, /2 \/ 20.*1 واجد شرایط.*حدود 2 دقیقه/);
  assert.equal(element("scan-progress").value, 2);
  assert.equal(element("network-label").textContent, "شبکه: mobile");
  context.current.progress.completed = 3;
  vm.runInContext("render(current)", context);
  assert.equal(element("scan-progress").value, 3);
  assert.equal(element("results").children.length, 0);
});

test("resume and recheck commands keep the exact mode and safe network label", () => {
  const {context, element} = client();
  context.current = {state: "missing", rows: [], summary: {}, source_reports: [], network: "mobile", progress: liveProgress({active: false, stage: "stopped", command: "recheck"})};
  vm.runInContext("render(current)", context);
  assert.equal(element("copy-resume").hidden, false);
  assert.equal(vm.runInContext("terminalCommand(current, true)", context), "python main.py recheck --mode mtproto --network mobile --resume");
  assert.equal(vm.runInContext("terminalCommand(current, false, true)", context), "python main.py recheck --mode mtproto --network mobile");
  context.current.network = "wifi; arbitrary command";
  assert.match(vm.runInContext("terminalCommand(current)", context), /--network default$/);
  assert.doesNotMatch(vm.runInContext("terminalCommand(current)", context), /arbitrary/);
});

test("stale heartbeat is never shown as an active scan or resumable instruction", () => {
  const {context, element} = client();
  context.current = {state: "missing", rows: [], summary: {}, source_reports: [], network: "mobile", progress_state: "stale", progress: liveProgress({updated_at: new Date(Date.now() - 600000).toISOString()})};
  vm.runInContext("render(current)", context);
  assert.match(element("progress-meta").textContent, /پیام فعالیت قدیمی/);
  assert.equal(element("copy-resume").hidden, true);
  assert.equal(element("scan-progress").value, 0);
});

test("history is plain past evidence and cannot create connection actions", () => {
  const {context, element} = client();
  context.current = {state: "missing", rows: [], summary: {}, source_reports: [], network: "mobile", history: [
    {mode: "mtproto", network: "mobile", server: "<script>alert(1)</script>", port: 443, last_tested: Date.now()/1000,
      successes: 7, attempts: 8, p95_ms: 120, qualified: true, historical_successes: 14, historical_attempts: 16, run_count: 2,
      tg_link: "tg://proxy"},
    {mode: "mtproto", network: "other", server: "other-network", port: 443}, null,
  ]};
  vm.runInContext("render(current)", context);
  const history = element("history-list");
  assert.equal(history.children.length, 1);
  assert.match(allText(history), /در آن زمان واجد شرایط.*14 \/ 16 در 2 اجرا/);
  assert.equal(history.children[0].children.length, 0);
  assert.equal(element("results").children.length, 0);
});

test("staged labels never promote baseline evidence below the final policy", () => {
  const {context, element} = client();
  const row = strongRow();
  row.strict_policy.min_successes = 7;
  context.current = {state: "ready", source_reports: [], rows: [row], summary: {
    strategy: "staged", stage: "stopped", provisional: true, interrupted: true,
    verification_profile: "telegram_session", tested: 1, verified: 1, strict_verified: 1, transport_reachable: 1,
  }};
  vm.runInContext("render(current, true)", context);
  assert.equal(element("count-verified").textContent, "0");
  assert.equal(element("results").children.length, 0);
  assert.match(element("partial-note").textContent, /موقت: بازآزمایی پایدار نهایی کامل نشده/);
});

test("source quality and cooldown remain visible with attribution counts", () => {
  const {context, element} = client();
  context.current = {state: "ready", rows: [], summary: {}, source_reports: [
    {name: "feed", status: "error", count: 0, checked: 9, qualified: 2, cooldown_until: Date.now()/1000 + 1800, error: "Cooling down"},
  ]};
  vm.runInContext("render(current, true)", context);
  assert.match(allText(element("source-list")), /9 آزموده.*2 واجد شرایط هنگام آزمون.*تلاش بعدی.*Cooling down/);
  assert.equal(element("source-summary").textContent, "منابع دریافت‌شده: 0 / 1 · 0 محتوای متفاوت · 0 حساب ناشر شناخته‌شده");
});


test("expired and undated evidence never creates usable links; fresh provisional remains explicit", () => {
  const {context} = client();
  for (const evidence_expires_at of [undefined, null, Date.now()/1000 - 1]) {
    context.row = strongRow({evidence_expires_at, provisional: true});
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
  context.row = strongRow({provisional: true});
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.tg_link);
  assert.match(allText(vm.runInContext("resultCard(row)", context)), /موقت/);
});

test("a cached connect click rechecks expiry even before polling refresh", () => {
  const {context} = client();
  context.row = strongRow();
  const card = vm.runInContext("resultCard(row)", context);
  const nodes = current => [current, ...current.children.flatMap(nodes)];
  const link = nodes(card).find(item => item.href === context.row.tg_link);
  assert.ok(link);
  context.row.evidence_expires_at = Date.now()/1000 - 1;
  let prevented = false;
  link.listeners.click({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
});

test("MTProto output format changes presentation without changing connection identity", () => {
  const {context, element} = client();
  context.row = strongRow();
  element("link-format").value = "https";
  element("link-format").listeners.change();
  const share = vm.runInContext("shareConnection(safeConnection(row))", context);
  assert.equal(share, context.row.tg_link.replace("tg://proxy", "https://t.me/proxy"));
  assert.equal(vm.runInContext("mode", context), "mtproto");
  const nodes = current => [current, ...current.children.flatMap(nodes)];
  const card = vm.runInContext("resultCard(row)", context);
  assert.equal(nodes(card).find(item => item.href).href, share);
  element("limit").value = "100";
  context.snapshot = {rows: Array.from({length: 20}, (_, index) => strongRow({rank: index + 1}))};
  assert.equal(vm.runInContext("filteredRows(snapshot).rows.length", context), 10);
});

test("theme switches persist without requiring network or account data", () => {
  const {context, element} = client();
  const saved = {};
  context.localStorage = {setItem(key, value) { saved[key] = value; }};
  element("theme-toggle").listeners.click();
  assert.equal(saved["revmamad-theme"], "light");
  assert.equal(element("theme-toggle").textContent, "حالت تیره");
  element("theme-toggle").listeners.click();
  assert.equal(saved["revmamad-theme"], "dark");
});


test("media reports render separate measurements without exposing account or connection fields", () => {
  const {context, element} = client();
  context.report = {schema_version: 1, experimental: true, network_profile: "default", status: "complete", created_at: Date.now()/1000, logout_status: "confirmed", observed_bytes: 80*1048576, downloaded_bytes: 80*1048576, budget_bytes: 100*1048576, elapsed_seconds: 10, ranked_candidate_ids: ["a".repeat(64)], phone: "PRIVATE_PHONE", results: [{candidate_id: "a".repeat(64), status: "complete", network_profile: "default", completed_rounds: 2, proof_expires_at: Date.now()/1000 - 5, median_mbps: 12, median_ttfb_ms: 100, stalls: 0, secret: "PRIVATE_SECRET", rounds: [{status: "complete", bytes: 8*1048576, mbps: 12, elapsed_seconds: 1, short_sample: true}]}]};
  vm.runInContext("renderMedia(report)", context);
  const text = allText(element("media-report"));
  assert.match(text, /رتبهٔ مدیا 1/);
  assert.match(text, /12.00 Mbps/);
  assert.match(text, /تأیید اتصال فعلی نیست/);
  assert.match(text, /اطمینان محدود/);
  assert.doesNotMatch(text, /PRIVATE|tg:\/\//);
  context.report.network_profile = "mobile";
  vm.runInContext("renderMedia(report)", context);
  assert.match(allText(element("media-report")), /گزارش مدیای معتبر موجود نیست/);
});


test("personal source mutation cannot be overwritten by an older in-flight library response", async () => {
  const {context, element} = client();
  let releaseOld;
  let lists = 0;
  const source = {id: "a".repeat(64), name: "personal", personal: true, enabled: true};
  context.fetch = async (_url, options) => {
    const action = JSON.parse(options.body).action;
    if (action === "list") {
      lists += 1;
      if (lists === 1) return new Promise(resolve => { releaseOld = () => resolve({ok: true, json: async () => ({sources: [source], favourites: []})}); });
      return {ok: true, json: async () => ({sources: [{...source, enabled: false}], favourites: []})};
    }
    return {ok: true, json: async () => action === "media_report" ? {report: null} : {saved: true}};
  };
  vm.runInContext("controlToken='test-token'; refreshControl=async()=>{}; refresh=()=>{}", context);
  const first = vm.runInContext("refreshLibrary()", context);
  await vm.runInContext("libraryAction({action:'source_toggle',id:'a'.repeat(64),enabled:false})", context);
  releaseOld();
  await first;
  assert.equal(lists, 2);
  assert.match(allText(element("library-sources")), /غیرفعال/);
  assert.equal(element("library-sources").children[0].children[1].textContent, "فعال کردن");
});
