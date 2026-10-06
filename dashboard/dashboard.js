"use strict";
let chart;
let generation = 0;
const byId = id => document.getElementById(id);
function cell(row, text, className = "") {
  const element = document.createElement("td");
  element.textContent = text;
  element.className = className;
  row.appendChild(element);
  return element;
}
const percentage = value => `${((Number(value) || 0) * 100).toFixed(1)}%`;
async function fetchJson(url) {
  const response = await fetch(url, {cache: "no-store"});
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
  return response.json();
}
async function load() {
  const current = ++generation;
  const query = new URLSearchParams({search: byId("search").value, sort_by: byId("sortBy").value,
    order: byId("order").value, limit: "200"});
  try {
    const [rows, stats] = await Promise.all([fetchJson(`/api/proxies?${query}`), fetchJson("/api/stats")]);
    if (current !== generation) return;
    byId("cards").replaceChildren();
    for (const [label, value] of [["Total Proxies", stats.total_proxies], ["Responding", stats.healthy_proxies],
      ["Avg Latency", `${stats.average_latency_ms ?? "-"} ms`], ["Best", stats.best_proxy?.server ?? "-"],
      ["Worst", stats.worst_proxy?.server ?? "-"]]) {
      const card = document.createElement("div"); card.className = "card";
      const caption = document.createElement("div"); caption.className = "label"; caption.textContent = label;
      const content = document.createElement("div"); content.className = "value"; content.textContent = value;
      card.append(caption, content); byId("cards").appendChild(card);
    }
    byId("rows").replaceChildren();
    for (const proxy of rows) {
      const row = document.createElement("tr");
      cell(row, proxy.server); cell(row, proxy.port); cell(row, percentage(proxy.score));
      cell(row, percentage(proxy.success_rate), proxy.success_rate > .8 ? "good" : "mid");
      cell(row, `${proxy.avg_latency_ms ?? "-"} ms`); cell(row, percentage(proxy.stability));
      cell(row, proxy.source);
      const linkCell = cell(row, "");
      if (typeof proxy.tg_link === "string" && proxy.tg_link.startsWith("tg://proxy?")) {
        const link = document.createElement("a"); link.className = "connect"; link.href = proxy.tg_link;
        link.textContent = "connect"; linkCell.appendChild(link);
      }
      byId("rows").appendChild(row);
    }
    byId("status").textContent = rows.length ? `Showing ${rows.length} results` : "No results. Run a proxy scan in Termux first.";
    if (typeof Chart !== "undefined") {
      const top = rows.filter(proxy => proxy.avg_latency_ms !== null).slice(0, 15);
      const data = {labels: top.map(proxy => proxy.server), datasets: [{label: "Latency (ms)",
        data: top.map(proxy => proxy.avg_latency_ms), backgroundColor: "#39ff9c"}]};
      if (chart) { chart.data = data; chart.update(); }
      else chart = new Chart(byId("chart"), {type: "bar", data, options: {responsive: true}});
    } else byId("chart").hidden = true;
  } catch (error) {
    if (current === generation) byId("status").textContent = `${error.message}. Keep the Termux dashboard session running.`;
  }
}
let searchTimer;
byId("search").addEventListener("input", () => {clearTimeout(searchTimer); searchTimer = setTimeout(load, 200);});
byId("sortBy").addEventListener("change", load);
byId("order").addEventListener("change", load);
load();
setInterval(load, 15000);
