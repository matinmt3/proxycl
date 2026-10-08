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

test("web copy actions accept default ports, SOCKS and IPv6", () => {
  const {context} = client();
  vm.runInContext("mode = 'web'", context);
  for (const row of [
    {server: "proxy.example", port: 80, protocol: "http", uri: "http://proxy.example:80"},
    {server: "proxy.example", port: 443, protocol: "https", uri: "https://proxy.example:443"},
    {server: "proxy.example", port: 1080, protocol: "socks5", uri: "socks5://proxy.example:1080"},
    {server: "2001:db8::1", port: 8080, protocol: "http", uri: "http://[2001:db8::1]:8080"},
  ]) {
    context.row = row;
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
  context.row = {server: "proxy.example", port: 443,
    tg_link: "tg://proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff"};
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
  assert.equal(element("source-list").children[0].textContent, "feed · ok · 9 candidates");
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
  assert.match(element("mode-title").textContent, /Telegram/);
  assert.equal(element("protocol-field").hidden, true);
  context.row = {server: "proxy.example", port: 443, secret: "00112233445566778899aabbccddeeff",
    web_link: "https://t.me/proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff",
    uri: "http://proxy.example:443"};
  assert.equal(vm.runInContext("safeConnection(row)", context), context.row.web_link);
  for (const prefix of ["http://t.me/proxy", "https://t.me.evil.example/proxy", "https://t.me:8443/proxy", "https://user@t.me/proxy", "https://t.me/other"]) {
    context.row.web_link = prefix + "?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff";
    assert.equal(vm.runInContext("safeConnection(row)", context), null);
  }
});

test("three keyboard tabs cycle and the HTTP/SOCKS mode keeps its own labels", () => {
  const {context, element} = client();
  const arrow = {key: "ArrowRight", preventDefault() {}};
  for (const expected of ["telegram", "web", "mtproto"]) {
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
  assert.match(element("partial-note").textContent, /Stopped scan.*partial results/i);
  assert.match(element("scan-meta").textContent, /Skipped: 5/);
  assert.match(element("scan-time-label").textContent, /STOPPED/);
  assert.equal(element("count-tested").textContent, "3");
  assert.equal(element("tested-description").textContent, "Candidates with samples");
  assert.match(element("results-title").textContent, /partial/i);
});

test("Telegram share links require a valid row secret and matching decoded identity", () => {
  const {context} = client();
  vm.runInContext("selectMode('telegram')", context);
  context.row = {server: "proxy.example", port: 443,
    web_link: "https://t.me/proxy?server=proxy.example&port=443&secret=00112233445566778899aabbccddeeff"};
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
