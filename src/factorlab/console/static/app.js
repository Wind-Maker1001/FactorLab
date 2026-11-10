"use strict";
const $ = (s) => document.querySelector(s);
const content = $("#content");
const state = { view: "overview", project: "", catalog: null, generation: 0 };
const titles = {
  overview: "研究总览",
  experiments: "实验浏览器",
  strategies: "真实策略结果",
  quality: "数据质量",
  evidence: "公司行为证据",
  reports: "报告与里程碑",
};
const labels = {
  strategy: "策略",
  year: "年度",
  symbol: "证券",
  industry: "行业",
  omitted_symbol: "剔除证券",
  accounts: "独立账户数",
  securities: "证券数",
  mean_return: "平均收益",
  median_return: "中位收益",
  profitable_account_fraction: "盈利账户比例",
  outperform_account_fraction: "跑赢基准比例",
  mean_exposure: "平均资金暴露",
  mean_cost_to_initial_capital: "交易成本 / 初始资金",
  cumulative_return: "账户收益",
  mean_annual_return: "剔除后平均年度收益",
  change_from_all: "相对全部样本变化",
  ready_rows: "合格证券日",
  rejected_rows: "拒绝证券日",
  observed_rows: "观察证券日",
  status: "状态",
  total_return: "总收益",
  total_return_status: "总收益状态",
};
const metricNames = [
  "mean_return",
  "median_return",
  "profitable_account_fraction",
  "outperform_account_fraction",
  "mean_exposure",
  "mean_cost_to_initial_capital",
];
const missing = (v) => v === null || v === undefined || v === "";
const text = (v) =>
  missing(v)
    ? "未知 / 不可计算"
    : typeof v === "object"
      ? JSON.stringify(v)
      : String(v);
function el(tag, value, cls) {
  const n = document.createElement(tag);
  if (value !== undefined) n.textContent = text(value);
  if (cls) n.className = cls;
  return n;
}
function append(parent, ...children) {
  children.forEach((n) => parent.append(n));
  return parent;
}
function panel(title) {
  const p = el("article", undefined, "panel");
  if (title) p.append(el("h2", title));
  content.append(p);
  return p;
}
function note(parent, message) {
  parent.append(el("p", message, "note"));
}
function button(label, action) {
  const b = el("button", label);
  b.type = "button";
  b.addEventListener("click", () => guard(action));
  return b;
}
function badge(value) {
  const b = el("span", "● " + value, "status");
  b.dataset.status = value;
  return b;
}
function link(report) {
  const a = el("a", report.name, "report");
  a.href = "/report?" + new URLSearchParams({ id: report.id });
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}
function jsonDetails(parent, title, data) {
  const d = el("details");
  append(d, el("summary", title), el("pre", JSON.stringify(data, null, 2)));
  parent.append(d);
  return d;
}
function stats(items) {
  const g = el("div", undefined, "grid");
  items.forEach(([label, value, sub]) => {
    const s = el("div", undefined, "stat");
    append(s, el("div", label, "muted"), el("div", value, "value"));
    if (sub) s.append(el("small", sub));
    g.append(s);
  });
  content.append(g);
}
function table(parent, rows, columns) {
  if (!rows.length) {
    parent.append(
      el("p", "没有匹配的记录。未保存的数据不会自动补造。", "empty"),
    );
    return;
  }
  const wrap = el("div", undefined, "table-wrap"),
    t = el("table"),
    head = el("tr");
  columns.forEach((k) => head.append(el("th", labels[k] || k)));
  const thead = el("thead");
  thead.append(head);
  t.append(thead);
  const body = el("tbody");
  rows.forEach((row) => {
    const tr = el("tr");
    columns.forEach((k) => {
      const td = el("td");
      const v = row[k];
      td.append(v instanceof Node ? v : document.createTextNode(text(v)));
      tr.append(td);
    });
    body.append(tr);
  });
  append(t, body);
  append(wrap, t);
  parent.append(wrap);
}
function select(parent, label, values, value, onchange) {
  const l = el("label", label),
    s = el("select");
  for (const [v, name] of values) {
    const o = el("option", name);
    o.value = v;
    s.append(o);
  }
  s.value = value;
  s.addEventListener("change", () => guard(() => onchange(s.value)));
  append(l, s);
  parent.append(l);
  return s;
}
async function api(path, params = {}) {
  const r = await fetch(path + "?" + new URLSearchParams(params));
  const d = await r.json();
  if (!r.ok) throw new Error(d.error);
  return d;
}
let busy = false;
async function guard(action) {
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll("button,input,select")].map(
    (n) => [n, n.disabled],
  );
  controls.forEach(([n]) => {
    n.disabled = true;
  });
  $("#message").textContent = "";
  try {
    await action();
  } catch (e) {
    $("#message").textContent = e.message;
  } finally {
    controls.forEach(([n, disabled]) => {
      n.disabled = disabled;
    });
    busy = false;
  }
}
const selectedProjects = () =>
  state.catalog.projects.filter(
    (p) => !state.project || p.id === state.project,
  );
function applicable(id) {
  if (state.project && state.project !== id) {
    panel().append(
      el("p", "当前项目与此视图不匹配，请切换研究项目筛选。", "empty"),
    );
    return false;
  }
  return true;
}
function projectReports(parent, id) {
  const p = state.catalog.projects.find((p) => p.id === id);
  if (!p) return;
  p.reports
    .filter((r) => r.name.endsWith(".md"))
    .forEach((r) => parent.append(link(r)));
  if (p.missing.length) note(parent, "缺失证据：" + p.missing.join("、"));
}
async function overview() {
  const [experiments, q] = await Promise.all([
    api("/api/experiments", {
      limit: 5,
      ...(state.project ? { project: state.project } : {}),
    }),
    api("/api/evidence", { project: "p6-d01" }),
  ]);
  stats([
    ["已发现实验定义", experiments.experiment_count, "不是报告数或策略账户数"],
    ["匹配运行记录", experiments.total, "来自显式配置的 Tracker"],
    ["研究类型", selectedProjects().length, "策略结果 / 数据质量 / 行为证据"],
    ["合格证券日", q.quality?.ready_rows, "P6-D01 公开质量摘要"],
  ]);
  const p = panel("研究任务状态");
  note(
    p,
    "软件验收、研究执行和证据阻塞相互独立。执行 SUCCEEDED 不自动升级为 RESEARCH_COMPLETED。",
  );
  table(
    p,
    selectedProjects().map((r) => ({
      project: r.title,
      status: badge(r.status),
      raw_status: r.raw_status,
      source: r.status_source,
      report: link(
        r.reports.find((x) => x.name === "REPORT.md") || {
          name: "未知",
          id: "missing",
        },
      ),
    })),
    ["project", "status", "raw_status", "source", "report"],
  );
  const recent = panel("最近实验");
  const rows = experiments.rows.filter(
    (r) => !state.project || r.project === state.project,
  );
  table(recent, rows, [
    "id",
    "project",
    "model",
    "date",
    "execution_status",
    "status",
  ]);
  if (!experiments.experiment_count)
    note(
      recent,
      "尚未配置本地 ExperimentTracker。公开研究摘要可独立浏览；不会把报告伪装成已发现的实验记录。",
    );
  const results = panel("最近研究结果 · 按已有证据展示");
  selectedProjects().forEach((project) => {
    const d = el("div");
    append(d, el("h3", project.title), badge(project.status));
    projectReports(d, project.id);
    results.append(d);
  });
  note(results, "未保存的研究时间为 UNKNOWN；不按文件修改时间推断最近执行。");
}
async function experiments() {
  const p = panel("实验记录与历史比较");
  const f = el("div", undefined, "filters");
  p.append(f);
  const filters = {};
  let offset = 0;
  const fields = [
    ["project", "任务", state.project],
    ["date", "日期前缀（YYYY-MM-DD）", ""],
    ["model", "模型", ""],
    ["strategy", "策略", ""],
  ];
  for (const [key, label, value] of fields) {
    const l = el("label", label),
      i = el("input");
    i.value = value;
    i.addEventListener("change", () => {
      filters[key] = i.value;
    });
    filters[key] = value;
    append(l, i);
    f.append(l);
  }
  select(
    f,
    "研究状态",
    [
      ["", "全部"],
      ...[
        "SOFTWARE_ACCEPTED",
        "RESEARCH_COMPLETED",
        "PARTIAL",
        "BLOCKED",
        "NOT_RUN",
        "UNKNOWN",
      ].map((s) => [s, s]),
    ],
    "",
    (v) => {
      filters.status = v;
    },
  );
  select(
    f,
    "执行状态",
    [
      ["", "全部"],
      ...["NOT_RUN", "UNKNOWN", "RUNNING", "SUCCEEDED", "FAILED"].map((s) => [
        s,
        s,
      ]),
    ],
    "",
    (v) => {
      filters.execution_status = v;
    },
  );
  const body = el("div"),
    detail = el("div"),
    comparison = el("div");
  const chosen = new Map();
  f.append(
    button("筛选", () => {
      offset = 0;
      return load();
    }),
  );
  p.append(body, detail, comparison);
  async function load() {
    const d = await api("/api/experiments", { ...filters, offset, limit: 25 });
    body.replaceChildren();
    table(
      body,
      d.rows.map((r) => {
        const c = el("input");
        c.type = "checkbox";
        c.setAttribute("aria-label", "比较 " + r.id);
        const key = r.store + ":" + r.id;
        c.checked = chosen.has(key);
        c.addEventListener("change", () => {
          if (c.checked) chosen.set(key, r);
          else chosen.delete(key);
        });
        return {
          ...r,
          choose: c,
          id: button(r.id, async () => {
            const data = await api("/api/experiment", {
              store: r.store,
              id: r.id,
            });
            detail.replaceChildren();
            const section = jsonDetails(
              detail,
              "实验详情 · 配置、身份、环境、证据",
              data,
            );
            section.open = true;
            const project = state.catalog.projects.find(
              (p) => p.id === r.project,
            );
            project?.reports.forEach((report) => detail.append(link(report)));
          }),
        };
      }),
      [
        "choose",
        "id",
        "project",
        "strategy",
        "model",
        "date",
        "execution_status",
        "status",
        "sha256",
      ],
    );
    pager(
      body,
      d,
      () => {
        offset -= 25;
        return load();
      },
      () => {
        offset += 25;
        return load();
      },
    );
  }
  comparison.append(
    button("比较已选运行（同一 Tracker，2–10 条）", async () => {
      const rows = [...chosen.values()];
      if (
        rows.length < 2 ||
        rows.length > 10 ||
        new Set(rows.map((r) => r.store)).size !== 1
      )
        throw new Error("请选择同一 Tracker 的 2–10 条运行。");
      const d = await api("/api/compare", {
        store: rows[0].store,
        ids: rows.map((r) => r.id).join(","),
      });
      comparison.querySelector("details")?.remove();
      jsonDetails(comparison, "比较结果 · 原有身份兼容规则", d).open = true;
    }),
  );
  note(
    p,
    "字段缺失为 UNKNOWN。复现信息仅展示记录，不执行命令，不跟随外部产物路径。",
  );
  await load();
}
function pager(parent, d, previous, next) {
  const bar = el("div", undefined, "pager");
  const prev = button("上一页", previous),
    following = button("下一页", next);
  prev.disabled = d.offset === 0;
  following.disabled = d.offset + d.limit >= d.total;
  append(
    bar,
    prev,
    el(
      "span",
      `第 ${d.total ? d.offset + 1 : 0}–${Math.min(d.offset + d.limit, d.total)} 条 / ${d.total}`,
      "muted",
    ),
    following,
  );
  parent.append(bar);
}
function percent(value) {
  return missing(value)
    ? "未知 / 不可计算"
    : (Number(value) * 100).toFixed(2) + "%";
}
function chart(parent, rows, metric, labelKeys) {
  const data = rows.filter(
    (row) => !missing(row[metric]) && Number.isFinite(Number(row[metric])),
  );
  if (!data.length) {
    parent.append(el("p", "所选指标不可计算。", "empty"));
    return;
  }
  const NS = "http://www.w3.org/2000/svg";
  const svg = (name, attrs = {}, value) => {
    const n = document.createElementNS(NS, name);
    Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
    if (value !== undefined) n.textContent = value;
    return n;
  };
  const width = 940,
    left = 270,
    right = 80,
    top = 38,
    height = top + data.length * 35 + 34;
  const values = data.map((r) => Number(r[metric]));
  let lo = Math.min(0, ...values),
    hi = Math.max(0, ...values);
  if (lo === hi) hi = lo + 0.01;
  const rough = (hi - lo) / 4,
    power = 10 ** Math.floor(Math.log10(rough)),
    step = [1, 2, 5, 10].find((n) => n * power >= rough) * power;
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const x = (v) => left + ((v - lo) / (hi - lo)) * (width - left - right);
  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": labels[metric] || metric,
    class: "chart",
  });
  root.append(
    svg(
      "title",
      {},
      (labels[metric] || metric) + "；单一指标，独立账户统计，数值见下表",
    ),
  );
  for (let i = 0; i <= Math.round((hi - lo) / step); i++) {
    const v = lo + step * i;
    root.append(
      svg("line", {
        x1: x(v),
        x2: x(v),
        y1: top - 10,
        y2: height - 28,
        class: "axis",
      }),
      svg("text", { x: x(v), y: 20, "text-anchor": "middle" }, percent(v)),
    );
  }
  root.append(
    svg("line", {
      x1: x(0),
      x2: x(0),
      y1: top - 10,
      y2: height - 28,
      class: "zero",
    }),
  );
  const tip = el(
    "div",
    "悬停或键盘聚焦圆点查看数值；同源原始值见下表。",
    "tooltip",
  );
  tip.setAttribute("aria-live", "polite");
  data.forEach((row, i) => {
    const y = top + i * 35 + 9,
      label = labelKeys
        .map((k) => row[k])
        .filter((v) => !missing(v))
        .join(" / "),
      value = Number(row[metric]);
    root.append(
      svg("text", { x: left - 16, y: y + 4, "text-anchor": "end" }, label),
      svg("circle", { cx: x(value), cy: y, r: 5, class: "mark" }),
    );
    const hit = svg("circle", {
      cx: x(value),
      cy: y,
      r: 14,
      class: "hit",
      tabindex: 0,
      role: "img",
      "aria-label": label + " " + percent(value),
    });
    hit.append(
      svg("title", {}, `${label}: ${percent(value)} (${row[metric]})`),
    );
    for (const evt of ["mouseenter", "focus"])
      hit.addEventListener(evt, () => {
        tip.textContent = `${label} · ${labels[metric] || metric}: ${percent(value)} · 原始值 ${row[metric]}`;
      });
    root.append(hit);
  });
  const scroll = el("div", undefined, "chart-scroll");
  scroll.append(root);
  append(parent, scroll, tip);
}
async function strategies() {
  if (!applicable("p6-r01")) return;
  const evidence = await api("/api/evidence", { project: "p6-r01" });
  stats([
    [
      "独立证券年度账户",
      evidence.real_accounts_run,
      "每策略 54 个独立账户；年度重置",
    ],
    ["证券数", evidence.securities, "不是全市场样本"],
    ["研究年度", evidence.years?.join(" / "), "不拼接成连续组合净值"],
    ["总收益", "不可计算", evidence.total_return_status],
  ]);
  const p = panel("策略跨样本比较");
  note(
    p,
    "RAW_PRICE_RETURN_NET_OF_TRADING_COSTS · 原始价格收益，已扣交易成本；不是分红再投资总收益。盈利/跑赢比例以独立账户为分母。行业为经济类别，不是历史时点认证分类。",
  );
  const f = el("div", undefined, "filters"),
    body = el("div");
  p.append(f, body);
  let name = "overall.csv",
    metric = "mean_return",
    offset = 0;
  const selected = {};
  let options = {};
  const datasets = [
    ["overall.csv", "总体 · 六策略"],
    ["annual.csv", "按年度"],
    ["industry.csv", "按行业"],
    ["leave_one_security_out.csv", "逐证券剔除敏感性"],
    ["account-metrics.csv", "独立证券年度账户"],
  ];
  const ds = select(f, "统计层级", datasets, name, (v) => {
    name = v;
    offset = 0;
    for (const k of Object.keys(selected)) delete selected[k];
    metric =
      name === "leave_one_security_out.csv"
        ? "mean_annual_return"
        : name === "account-metrics.csv"
          ? "cumulative_return"
          : "mean_return";
    return load(true);
  });
  ds.id = "strategy-dataset";
  const dynamic = el("div", undefined, "filters");
  p.insertBefore(dynamic, body);
  async function load(reset = false) {
    const d = await api("/api/table", {
      id: "p6-r01:" + name,
      ...selected,
      offset,
      limit: 30,
    });
    if (state.view !== "strategies") return;
    options = d.options;
    if (reset) {
      dynamic.replaceChildren();
      const metrics =
        name === "leave_one_security_out.csv"
          ? ["mean_annual_return", "change_from_all"]
          : name === "account-metrics.csv"
            ? [
                "cumulative_return",
                "relative_buy_hold_return",
                "average_capital_exposure",
                "cost_to_initial_capital",
              ]
            : metricNames;
      const m = select(
        dynamic,
        "图表指标",
        metrics.map((k) => [k, labels[k] || k]),
        metric,
        (v) => {
          metric = v;
          return load();
        },
      );
      m.id = "strategy-metric";
      for (const key of [
        "strategy",
        "year",
        "symbol",
        "industry",
        "omitted_symbol",
      ])
        if (options[key])
          select(
            dynamic,
            labels[key],
            [["", "全部"], ...options[key].map((v) => [v, v])],
            selected[key] || "",
            (v) => {
              selected[key] = v;
              offset = 0;
              return load();
            },
          );
    }
    body.replaceChildren();
    body.append(el("h3", (labels[metric] || metric) + " · 当前筛选页"));
    chart(body, d.rows, metric, [
      "strategy",
      "year",
      "industry",
      "symbol",
      "omitted_symbol",
    ]);
    table(body, d.rows, d.columns);
    pager(
      body,
      d,
      () => {
        offset -= 30;
        return load();
      },
      () => {
        offset += 30;
        return load();
      },
    );
  }
  await load(true);
  projectReports(p, "p6-r01");
  jsonDetails(p, "原始身份、研究配置与证据状态", evidence);
}
async function csvBrowser(parent, id, title, filterKeys = []) {
  const outer = el("div");
  parent.append(outer);
  outer.append(el("h3", title));
  const f = el("div", undefined, "filters"),
    body = el("div");
  outer.append(f, body);
  let offset = 0;
  const selected = {};
  async function load(first = false) {
    const d = await api("/api/table", { id, ...selected, offset, limit: 25 });
    if (first)
      for (const key of filterKeys)
        if (d.options[key])
          select(
            f,
            labels[key] || key,
            [["", "全部"], ...d.options[key].map((v) => [v, v])],
            "",
            (v) => {
              selected[key] = v;
              offset = 0;
              return load();
            },
          );
    body.replaceChildren();
    table(body, d.rows, d.columns);
    pager(
      body,
      d,
      () => {
        offset -= 25;
        return load();
      },
      () => {
        offset += 25;
        return load();
      },
    );
  }
  await load(true);
}
async function quality() {
  if (!applicable("p6-d01")) return;
  const d = await api("/api/evidence", { project: "p6-d01" }),
    q = d.quality;
  stats([
    ["固定证券", q?.securities?.length, "原始固定研究样本"],
    ["合格证券日", q?.ready_rows],
    ["拒绝证券日", q?.rejected_rows],
    [
      "合格连续区间",
      q?.securities?.reduce((n, s) => n + s.intervals.length, 0),
    ],
  ]);
  const p = panel("年度覆盖与不可用原因");
  note(
    p,
    "仅研究输入质量，不是正式历史 PIT 认证。历史上市/退市、历史时点可用性与 HFQ 仍受原报告边界限制。",
  );
  await csvBrowser(p, "p6-d01:year-coverage.csv", "证券年度覆盖", [
    "year",
    "symbol",
  ]);
  if (q) {
    jsonDetails(p, "各证券拒绝原因与合格区间", q.securities);
    jsonDetails(p, "不可用原因汇总", q.rejected_reasons);
  }
  jsonDetails(p, "Manifest SHA256 与证据状态", {
    ...d,
    quality: undefined,
  }).open = true;
  projectReports(p, "p6-d01");
}
async function evidence() {
  if (!applicable("p6-e01")) return;
  const d = await api("/api/evidence", { project: "p6-e01" });
  stats([
    ["总收益", "不可计算", d.total_return_status],
    [
      "证据状态",
      state.catalog.projects.find((p) => p.id === "p6-e01")?.status ||
        "UNKNOWN",
      d.status,
    ],
  ]);
  const p = panel("已核实事件与税前分红权益");
  note(
    p,
    "税前分红权益不是实际到账或可再投资现金。仅已核实的两个现金事件；全类别覆盖、账户税率和到账合同尚未闭合。null 不是 0。",
  );
  await csvBrowser(p, "p6-e01:EVENT_EVIDENCE.csv", "现金分红事件");
  const rows = Object.entries(d.strategies || {}).map(([strategy, v]) => ({
    strategy,
    bounded_gross_entitlement: v.bounded_gross_entitlement,
    total_return_gross: v.total_return_gross,
    total_return_net: v.total_return_net,
    total_return_status: v.total_return_status,
  }));
  table(p, rows, [
    "strategy",
    "bounded_gross_entitlement",
    "total_return_gross",
    "total_return_net",
    "total_return_status",
  ]);
  await csvBrowser(p, "p6-e01:FIELD_COVERAGE.csv", "公司行为字段覆盖");
  jsonDetails(p, "缺失证据与阻塞项", d.blockers).open = true;
  jsonDetails(p, "原始事件权益、null 字段与身份", d);
  projectReports(p, "p6-e01");
}
async function reports() {
  for (const project of selectedProjects()) {
    const p = panel(project.title);
    p.append(badge(project.status));
    project.reports.forEach((r) => p.append(link(r)));
    if (project.missing.length) note(p, "缺失：" + project.missing.join("、"));
  }
  const p = panel("PR / CI / 里程碑 · 本地记录");
  note(p, "本地记录和显式链接不是 GitHub 实时状态；不会认证、同步或自动轮询。");
  const records = state.catalog.milestones.filter(
    (m) => !state.project || m.project === state.project,
  );
  table(
    p,
    records.map((m) => {
      let url;
      try {
        url = new URL(m.url);
      } catch {
        url = null;
      }
      const a = el("a", m.pr || "GitHub");
      if (url?.protocol === "https:" && url.hostname === "github.com") {
        a.href = url.href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
      const report = selectedProjects()
        .flatMap((p) => p.reports)
        .find((r) => r.id === m.report);
      return { ...m, report: report ? link(report) : "UNKNOWN", link: a };
    }),
    [
      "project",
      "pr",
      "title",
      "status",
      "snapshot_at",
      "source",
      "report",
      "experiment_identity",
      "link",
    ],
  );
}
const views = { overview, experiments, strategies, quality, evidence, reports };
async function render() {
  const generation = ++state.generation;
  content.replaceChildren();
  $("#page-title").textContent = titles[state.view];
  document.querySelectorAll("nav button").forEach((b) => {
    if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  await views[state.view]();
  if (generation !== state.generation) return;
}
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () =>
    guard(async () => {
      state.view = b.dataset.view;
      await render();
    }),
  ),
);
$("#project").addEventListener("change", () =>
  guard(async () => {
    state.project = $("#project").value;
    await render();
  }),
);
guard(async () => {
  state.catalog = await api("/api/overview");
  state.catalog.projects.forEach((p) => {
    const o = el("option", p.title);
    o.value = p.id;
    $("#project").append(o);
  });
  await render();
});
