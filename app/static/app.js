/* 闭环供水管网平衡台 —— 原生前端（无框架、无构建步骤） */
"use strict";

const SVG_W = 900;
const SVG_H = 440;
const NODE_R = 16;

const state = {
  nodes: [],
  pipes: [],
  minHead: "0",
  result: null,
  outage: null,       // 最近一次逐根停管分析结果
  pos: {},            // nodeId -> {x, y, pinned}
  temp: 0,            // 力导向布局“温度”，>0 时持续迭代
  rafId: null,
  dragId: null,
};

/* ------------------------------ 工具函数 ------------------------------ */
const $ = (sel) => document.querySelector(sel);

function uid(prefix, existing) {
  let k = existing.size + 1;
  while (existing.has(prefix + k)) k++;
  existing.add(prefix + k);
  return prefix + k;
}

function fmt(x, digits = 4) {
  if (x === undefined || x === null || Number.isNaN(x)) return "—";
  if (Object.is(x, -0)) x = 0;
  if (Math.abs(x) >= 1e5 || (x !== 0 && Math.abs(x) < 1e-4)) {
    return x.toExponential(3);
  }
  return Number(x.toFixed(digits)).toString();
}

function nodeTypeLabel(t) {
  return t === "source" ? "水源" : "用水";
}

/* ------------------------------ 示例/重置 ------------------------------ */
function loadSample() {
  state.nodes = [
    { id: "S1", type: "source", head: "40", demand: "" },
    { id: "N1", type: "demand", head: "", demand: "1.2" },
    { id: "N2", type: "demand", head: "", demand: "0.8" },
    { id: "N3", type: "demand", head: "", demand: "1.5" },
    { id: "N4", type: "demand", head: "", demand: "0.6" },
  ];
  state.pipes = [
    { id: "P1", start: "S1", end: "N1", r: "2.0" },
    { id: "P2", start: "N1", end: "N2", r: "1.5" },
    { id: "P3", start: "N2", end: "N3", r: "1.0" },
    { id: "P4", start: "N3", end: "S1", r: "3.0" },
    { id: "P5", start: "N1", end: "N4", r: "2.5" },
    { id: "P6", start: "N4", end: "N3", r: "1.2" },
    { id: "P7", start: "S1", end: "N2", r: "4.0" },
  ];
  state.minHead = "28";
  state.result = null;
  initPositions(true);
  renderAll();
  solve();
}

function resetAll() {
  state.nodes = [
    { id: "S", type: "source", head: "30", demand: "" },
    { id: "A", type: "demand", head: "", demand: "1" },
  ];
  state.pipes = [{ id: "P1", start: "S", end: "A", r: "1" }];
  state.minHead = "0";
  state.result = null;
  initPositions(true);
  renderAll();
  setBanner("idle", "填写数据后点击“求解”。所有数值必须为有限数，且管网为无向连通图。");
}

/* ------------------------------ 表格渲染 ------------------------------ */
function renderNodeRows() {
  const tb = $("#nodeTbody");
  tb.innerHTML = "";
  state.nodes.forEach((node, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="cell-id"><input type="text" data-k="id" value="${escapeAttr(node.id)}" /></td>
      <td>
        <select data-k="type">
          <option value="demand"${node.type === "demand" ? " selected" : ""}>用水（需水量）</option>
          <option value="source"${node.type === "source" ? " selected" : ""}>水源（固定水头）</option>
        </select>
      </td>
      <td class="num-col"><input type="number" step="any" data-k="head"
          value="${escapeAttr(node.head ?? "")}"
          ${node.type !== "source" ? "disabled placeholder=\"仅水源填写\"" : ""} /></td>
      <td class="num-col"><input type="number" step="any" data-k="demand"
          value="${escapeAttr(node.demand ?? "")}"
          ${node.type !== "demand" ? "disabled placeholder=\"仅用水节点填写\"" : ""} /></td>
      <td class="op-col"><button class="del-btn" title="删除节点"
          ${state.nodes.length <= 2 ? "disabled" : ""}>✕</button></td>`;

    tr.querySelectorAll("[data-k]").forEach((el) => {
      el.addEventListener("input", () => {
        const key = el.dataset.k;
        if (key === "id") {
          const oldId = node.id;
          const newId = el.value.trim();
          node.id = newId || oldId;
          if (newId && newId !== oldId) {
            markDirty();
            // 同步管段端点下拉
            state.pipes.forEach((pp) => {
              if (pp.start === oldId) pp.start = newId;
              if (pp.end === oldId) pp.end = newId;
            });
            if (state.pos[oldId]) {
              state.pos[newId] = state.pos[oldId];
              delete state.pos[oldId];
            }
            node.id = newId;
            renderPipeRows();
            draw();
          }
        } else if (key === "type") {
          node.type = el.value;
          node.head = node.type === "source" ? (node.head || "30") : "";
          node.demand = node.type === "demand" ? (node.demand || "0") : "";
          // 全局只能有一个水源：切走其它水源
          if (node.type === "source") {
            state.nodes.forEach((other) => {
              if (other !== node && other.type === "source") {
                other.type = "demand";
                other.demand = "0";
                other.head = "";
              }
            });
          }
          renderNodeRows();
        } else {
          node[key] = el.value;
          markDirty();
        }
      });
    });
    tr.querySelector(".del-btn").addEventListener("click", () => deleteNode(idx));
    tb.appendChild(tr);
  });
  $("#nodeCount").textContent =
    `${state.nodes.length}/${10} 个 · 水源 ${state.nodes.filter((n) => n.type === "source").length} 个`;
  $("#addNodeBtn").disabled = state.nodes.length >= 10;
}

function nodeOptions(selected) {
  return state.nodes.map((n) =>
    `<option value="${escapeAttr(n.id)}"${n.id === selected ? " selected" : ""}>${escapeHtml(n.id)}</option>`
  ).join("");
}

function renderPipeRows() {
  const tb = $("#pipeTbody");
  tb.innerHTML = "";
  state.pipes.forEach((pipe, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="cell-id"><input type="text" data-k="id" value="${escapeAttr(pipe.id)}" /></td>
      <td><select data-k="start">${nodeOptions(pipe.start)}</select></td>
      <td><select data-k="end">${nodeOptions(pipe.end)}</select></td>
      <td class="num-col"><input type="number" step="any" min="0" data-k="r" value="${escapeAttr(pipe.r)}" /></td>
      <td class="op-col"><button class="del-btn" title="删除管段"
          ${state.pipes.length <= 1 ? "disabled" : ""}>✕</button></td>`;

    tr.querySelectorAll("[data-k]").forEach((el) => {
      el.addEventListener("input", () => {
        pipe[el.dataset.k] = el.type === "select-one" ? el.value : el.value;
        markDirty();
        heat(0.15);
        draw();
      });
      el.addEventListener("change", () => draw());
    });
    tr.querySelector(".del-btn").addEventListener("click", () => deletePipe(idx));
    tb.appendChild(tr);
  });
  $("#pipeCount").textContent = `${state.pipes.length}/${16} 根`;
  $("#addPipeBtn").disabled = state.pipes.length >= 16;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const escapeAttr = escapeHtml;

function addNode() {
  if (state.nodes.length >= 10) return;
  const existing = new Set(state.nodes.map((n) => n.id));
  const id = uid("N", existing);
  const node = { id, type: "demand", head: "", demand: "0" };
  state.nodes.push(node);
  placeOnCircle(id);
  heat(0.5);
  renderAll();
}

function deleteNode(idx) {
  if (state.nodes.length <= 2) return;
  const id = state.nodes[idx].id;
  state.nodes.splice(idx, 1);
  state.pipes = state.pipes.filter((p) => p.start !== id && p.end !== id);
  delete state.pos[id];
  if (state.pipes.length === 0 && state.nodes.length >= 2) {
    state.pipes.push({
      id: "P1", start: state.nodes[0].id, end: state.nodes[1].id, r: "1",
    });
  }
  heat(0.4);
  renderAll();
}

function addPipe() {
  if (state.pipes.length >= 16 || state.nodes.length < 2) return;
  const existing = new Set(state.pipes.map((p) => p.id));
  const id = uid("P", existing);
  // 默认选两个不同的节点
  const a = state.nodes[0].id;
  const b = state.nodes.find((n) => n.id !== a).id;
  state.pipes.push({ id, start: a, end: b, r: "1" });
  heat(0.3);
  renderAll();
}

function deletePipe(idx) {
  if (state.pipes.length <= 1) return;
  state.pipes.splice(idx, 1);
  heat(0.2);
  renderAll();
}

function renderAll() {
  markDirty();
  renderNodeRows();
  renderPipeRows();
  $("#minHeadInput").value = state.minHead;
  draw();
}

/* ------------------------------ 网络力图 ------------------------------ */
function initPositions(heatUp) {
  state.pos = {};
  const cx = SVG_W / 2, cy = SVG_H / 2;
  const R = Math.min(SVG_W, SVG_H) * 0.36;
  const n = state.nodes.length;
  state.nodes.forEach((node, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(n, 1);
    state.pos[node.id] = {
      x: cx + R * Math.cos(ang) + (Math.random() - 0.5) * 14,
      y: cy + R * Math.sin(ang) * 0.8 + (Math.random() - 0.5) * 14,
    };
  });
  if (heatUp) heat(1.0);
}

function placeOnCircle(id) {
  const cx = SVG_W / 2, cy = SVG_H / 2;
  const R = Math.min(SVG_W, SVG_H) * 0.36;
  const ang = Math.random() * 2 * Math.PI;
  state.pos[id] = { x: cx + R * Math.cos(ang), y: cy + R * 0.8 * Math.sin(ang) };
}

function heat(t) {
  state.temp = Math.max(state.temp, t);
  if (state.rafId === null) state.rafId = requestAnimationFrame(tick);
}

function tick() {
  const nodes = state.nodes;
  const pos = state.pos;
  const cx = SVG_W / 2, cy = SVG_H / 2;
  const forces = Object.fromEntries(nodes.map((n) => [n.id, { x: 0, y: 0 }]));
  const k = 150; // 弹簧理想长度

  // 管段弹簧
  state.pipes.forEach((p) => {
    const a = pos[p.start], b = pos[p.end];
    if (!a || !b) return;
    let dx = b.x - a.x, dy = b.y - a.y;
    let dist = Math.hypot(dx, dy) || 0.01;
    const f = (dist - k) * 0.02;
    const ux = dx / dist, uy = dy / dist;
    forces[p.start].x += ux * f;
    forces[p.start].y += uy * f;
    forces[p.end].x -= ux * f;
    forces[p.end].y -= uy * f;
  });

  // 节点互斥
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = pos[nodes[i].id], b = pos[nodes[j].id];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = dx * dx + dy * dy || 1; }
      const d = Math.sqrt(d2);
      const f = 2600 / d2;
      const ux = dx / d, uy = dy / d;
      forces[nodes[i].id].x += ux * f;
      forces[nodes[i].id].y += uy * f;
      forces[nodes[j].id].x -= ux * f;
      forces[nodes[j].id].y -= uy * f;
    }
  }

  // 向心
  nodes.forEach((n) => {
    const pt = pos[n.id];
    forces[n.id].x += (cx - pt.x) * 0.004;
    forces[n.id].y += (cy - pt.y) * 0.004;
  });

  let moved = 0;
  nodes.forEach((n) => {
    if (n.id === state.dragId) return;
    const pt = pos[n.id];
    const step = Math.min(state.temp * 40, 18);
    const fx = forces[n.id].x, fy = forces[n.id].y;
    const mag = Math.hypot(fx, fy) || 1;
    pt.x += (fx / mag) * Math.min(mag, step);
    pt.y += (fy / mag) * Math.min(mag, step);
    pt.x = Math.max(30, Math.min(SVG_W - 30, pt.x));
    pt.y = Math.max(28, Math.min(SVG_H - 26, pt.y));
    moved += Math.min(mag, step);
  });

  state.temp *= 0.94;
  draw();
  if (state.temp > 0.02 && moved > 0.02) {
    state.rafId = requestAnimationFrame(tick);
  } else {
    state.temp = 0;
    state.rafId = null;
    draw();
  }
}

/* ------------------------------ SVG 绘制 ------------------------------ */
const SVG_NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}) {
  const e = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function draw() {
  const svg = $("#networkSvg");
  svg.setAttribute("viewBox", `0 0 ${SVG_W} ${SVG_H}`);
  svg.innerHTML = "";

  // defs：正/负流向两个箭头
  const defs = el("defs");
  [["arrow-pos", "#1466d0"], ["arrow-neg", "#d97a1e"],
   ["arrow-idle", "#8a97a5"]].forEach(([id, color]) => {
    const m = el("marker", {
      id, viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    const path = el("path", { d: "M0,0 L10,5 L0,10 z", fill: color });
    m.appendChild(path);
    defs.appendChild(m);
  });
  svg.appendChild(defs);

  const flows = state.result ? state.result.flows : null;
  const heads = state.result ? state.result.heads : null;
  const below = state.result ? new Set(state.result.below_min_head) : new Set();

  // 先画管段（线 + 标签）
  state.pipes.forEach((p) => {
    const a = state.pos[p.start], b = state.pos[p.end];
    if (!a || !b) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.hypot(dx, dy) || 1;
    const ux = dx / dist, uy = dy / dist;
    // 收缩到节点圆边缘
    const x1 = a.x + ux * NODE_R, y1 = a.y + uy * NODE_R;
    const x2 = b.x - ux * NODE_R, y2 = b.y - uy * NODE_R;

    let q = null;
    if (flows && flows[p.id] !== undefined) q = flows[p.id];
    let cls = "edge-line edge-idle", marker = "url(#arrow-idle)";
    // 物理起点/终点（按实际流向）
    let fx1 = x1, fy1 = y1, fx2 = x2, fy2 = y2;
    if (q !== null && Math.abs(q) > 1e-9) {
      if (q > 0) {
        cls = "edge-line edge-pos"; marker = "url(#arrow-pos)";
      } else {
        cls = "edge-line edge-neg"; marker = "url(#arrow-neg)";
        fx1 = x2; fy1 = y2; fx2 = x1; fy2 = y1; // 箭头由实际上游指向下游
      }
    }
    if (q !== null && Math.abs(q) <= 1e-9) {
      marker = null; // 零流量不画箭头
    }
    const lineAttrs = { class: cls, x1: fx1, y1: fy1, x2: fx2, y2: fy2 };
    if (marker) lineAttrs.markerEnd = marker;
    svg.appendChild(el("line", lineAttrs));

    // 标签放在管段中点，略向上法向偏移
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const nx = -uy, ny = ux;
    const lx = mx + nx * 9, ly = my + ny * 9 + 3;
    const text = el("text", { x: lx, y: ly, class: "edge-label",
      "text-anchor": "middle" });
    text.textContent = q !== null ? `${p.id}: ${fmt(q, 3)}` : p.id;
    svg.appendChild(text);
  });

  // 再画节点
  state.nodes.forEach((node) => {
    const pt = state.pos[node.id];
    if (!pt) return;
    const isLow = below.has(node.id);
    const cls = "node-circle " +
      (isLow ? "node-low" : node.type === "source" ? "node-source" : "node-demand") +
      (state.dragId === node.id ? " dragging" : "");
    const circle = el("circle", {
      class: cls, cx: pt.x, cy: pt.y, r: NODE_R, "data-id": node.id,
    });
    svg.appendChild(circle);

    const labelCls = "node-label" + (node.type === "source" && !isLow ? " light" : "");
    const t1 = el("text", { x: pt.x, y: pt.y - 24, class: labelCls });
    t1.textContent = node.id + (node.type === "source" ? " ◈" : "");
    svg.appendChild(t1);

    if (heads && heads[node.id] !== undefined) {
      const t2 = el("text", { x: pt.x, y: pt.y + 4, class: "node-sublabel",
        "text-anchor": "middle" });
      t2.textContent = `H=${fmt(heads[node.id], 3)}`;
      t2.setAttribute("fill", node.type === "source" && !isLow ? "#ffffff" : "#1f2933");
      svg.appendChild(t2);
    }
  });
}

/* ------------------------------ 节点拖拽 ------------------------------ */
function svgPoint(evt) {
  const svg = $("#networkSvg");
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  const ctm = svg.getScreenCTM();
  if (!ctm) return { x: 0, y: 0 };
  const p = pt.matrixTransform(ctm.inverse());
  return { x: p.x, y: p.y };
}

$("#networkSvg").addEventListener("pointerdown", (evt) => {
  const c = evt.target.closest("circle.node-circle");
  if (!c) return;
  const id = c.dataset.id;
  state.dragId = id;
  c.classList.add("dragging");
  $("#networkSvg").setPointerCapture(evt.pointerId);
  const p = svgPoint(evt);
  const pt = state.pos[id];
  pt.x = p.x; pt.y = p.y;
  heat(0.1);
});

$("#networkSvg").addEventListener("pointermove", (evt) => {
  if (!state.dragId) return;
  const p = svgPoint(evt);
  const pt = state.pos[state.dragId];
  pt.x = Math.max(24, Math.min(SVG_W - 24, p.x));
  pt.y = Math.max(22, Math.min(SVG_H - 20, p.y));
  heat(0.08);
});

function endDrag(evt) {
  if (!state.dragId) return;
  state.dragId = null;
  draw();
}
$("#networkSvg").addEventListener("pointerup", endDrag);
$("#networkSvg").addEventListener("pointercancel", endDrag);

/* ------------------------------ 求解 ------------------------------ */
function parseNum(raw, label, errors, { nonneg = false, positive = false } = {}) {
  const s = String(raw ?? "").trim();
  if (s === "") { errors.push(`${label}未填写。`); return null; }
  const v = Number(s);
  if (!Number.isFinite(v)) { errors.push(`${label}必须是有限数值。`); return null; }
  if (positive && v <= 0) errors.push(`${label}必须为正数，当前为 ${v}。`);
  if (nonneg && v < 0) errors.push(`${label}不能为负值，当前为 ${v}。`);
  return v;
}

function buildPayload() {
  const errors = [];
  if (state.nodes.length < 2) errors.push("至少需要 2 个节点。");
  const sourceNodes = state.nodes.filter((n) => n.type === "source");
  if (sourceNodes.length !== 1) {
    errors.push(`必须恰好有 1 个水源节点，当前为 ${sourceNodes.length} 个。`);
  }

  const nodes = state.nodes.map((n) => {
    if (!n.id.trim()) errors.push("存在未填写编号的节点。");
    if (n.type === "source") {
      const head = parseNum(n.head, `水源节点“${n.id}”的固定水头`, errors);
      return { id: n.id.trim(), type: "source", head };
    }
    const demand = parseNum(n.demand, `用水节点“${n.id}”的需水量`, errors, { nonneg: true });
    return { id: n.id.trim(), type: "demand", demand };
  });

  const ids = new Set();
  state.nodes.forEach((n) => {
    if (!n.id.trim()) return;
    if (ids.has(n.id)) errors.push(`节点编号“${n.id}”重复。`);
    ids.add(n.id);
  });

  const pipeIds = new Set();
  const edgeKeys = new Set();
  const pipes = state.pipes.map((p) => {
    if (!p.id.trim()) errors.push("存在未填写编号的管段。");
    if (pipeIds.has(p.id)) errors.push(`管段编号“${p.id}”重复。`);
    pipeIds.add(p.id);
    if (!p.start || !p.end) errors.push(`管段“${p.id}”的起点与终点都必须选择。`);
    if (p.start === p.end) errors.push(`管段“${p.id}”不允许起点与终点相同。`);
    const key = [p.start, p.end].sort().join("|");
    if (edgeKeys.has(key)) errors.push(`管段“${p.id}”与其它管段端点重复（${p.start}–${p.end}）。`);
    edgeKeys.add(key);
    const rr = parseNum(p.r, `管段“${p.id}”的阻力 r`, errors, { positive: true });
    return { id: p.id.trim(), start: p.start, end: p.end, r: rr };
  });

  const minHead = parseNum(state.minHead, "最低水头限值", errors);
  return { payload: { nodes, pipes, min_head: minHead }, errors };
}

async function solve() {
  const { payload, errors } = buildPayload();
  if (errors.length) {
    state.result = null;
    clearOutage();
    $("#resultsPanel").hidden = true;
    draw();
    setBanner("err", "输入不合法：\n" + dedupe(errors).map((e) => `• ${e}`).join("\n"));
    return;
  }

  // 重新求解即令旧的停管分析失效（成功后可再点“停管分析”）
  clearOutage();
  setBanner("idle", "求解中……");
  let resp, data;
  try {
    resp = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await resp.json();
  } catch (err) {
    setBanner("err", `无法连接求解服务：${err.message}`);
    return;
  }

  if (!resp.ok || !data.ok) {
    state.result = null;
    $("#resultsPanel").hidden = true;
    draw();
    setBanner("err", data.error || `求解失败（HTTP ${resp.status}）。`);
    return;
  }

  state.result = data.result;
  renderResults(state.result);
  draw();
  const r = state.result;
  if (r.below_min_head.length) {
    setBanner("warn",
      `求解收敛：水源“${r.source_node}”总供水量 ${fmt(r.source_supply)}（= 总需水量 ${fmt(r.total_demand)}，` +
      `供耗差 ${fmt(r.balance_residual, 2)}）。有 ${r.below_min_head.length} 个节点低于最低水头 ` +
      `${fmt(r.min_head)}：${r.below_min_head.join("、")}。`);
  } else {
    setBanner("ok",
      `求解收敛：水源“${r.source_node}”总供水量 ${fmt(r.source_supply)} = 总需水量 ${fmt(r.total_demand)}；` +
      `所有节点水头均不低于 ${fmt(r.min_head)}。节点守恒/管段压降均通过 1e-6 容差校验。`);
  }
}

function dedupe(arr) { return [...new Set(arr)]; }

/* ------------------------------ 逐根停管分析 ------------------------------ */
async function analyzeOutages() {
  const { payload, errors } = buildPayload();
  if (errors.length) {
    clearOutage();
    draw();
    setBanner("err", "输入不合法：\n" + dedupe(errors).map((e) => `• ${e}`).join("\n"));
    return;
  }

  setBanner("idle", "停管分析中……（逐根移除并重新求解）");
  let resp, data;
  try {
    resp = await fetch("/api/analyze-outages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await resp.json();
  } catch (err) {
    setBanner("err", `无法连接求解服务：${err.message}`);
    return;
  }

  if (!resp.ok || !data.ok) {
    clearOutage();
    setBanner("err", data.error || `停管分析失败（HTTP ${resp.status}）。`);
    return;
  }

  // 分析内含基线求解，直接作为当前稳态结果展示
  state.result = data.analysis.baseline;
  state.outage = data.analysis;
  renderResults(state.result);
  renderOutage(state.outage);
  draw();
  const c = state.outage.counts;
  setBanner(c.disconnected || c.not_converged ? "warn" : "ok",
    `停管分析完成：共 ${state.outage.scenarios.length} 个情景（每根管段一个）——` +
    `断联失供 ${c.disconnected} 个、连通可解 ${c.connected} 个、不收敛 ${c.not_converged} 个；` +
    `已按风险排序（断联按失供量降序 → 连通按最低水头升序 → 不收敛排最后）。`);
}

function outageStatusCell(s) {
  if (s.status === "disconnected") {
    return `<span class="pill pill-low">断联失供</span>`;
  }
  if (s.status === "not_converged") {
    return `<span class="pill pill-nc" title="${escapeAttr(s.error)}">不收敛</span>`;
  }
  return `<span class="pill pill-ok">连通可解</span>`;
}

function renderOutage(a) {
  $("#outagePanel").hidden = false;
  const c = a.counts;
  const cards = [
    { k: "情景总数（= 管段数）", v: String(a.scenarios.length) },
    { k: "断联失供情景", v: String(c.disconnected), cls: c.disconnected ? "bad" : "good" },
    { k: "连通可解情景", v: String(c.connected) },
    { k: "不收敛情景", v: String(c.not_converged), cls: c.not_converged ? "bad" : "good" },
    { k: "当前最低水头限值", v: fmt(a.min_head) },
  ];
  $("#outageSummary").innerHTML = cards.map((cc) =>
    `<div class="summary-card ${cc.cls || ""}"><div class="k">${cc.k}</div><div class="v">${cc.v}</div></div>`
  ).join("");

  $("#outageTbody").innerHTML = a.scenarios.map((s) => {
    const pipeDesc = `${escapeHtml(s.pipe_id)}` +
      ` <span class="hint">(${escapeHtml(s.removed_pipe.start)} → ${escapeHtml(s.removed_pipe.end)})</span>`;
    let unserved = "—", minHeadCell = "—", below = "—", newly = "—";
    let rowCls = "";
    if (s.status === "disconnected") {
      rowCls = "row-disconnected";
      unserved = `<span class="pill pill-low">${s.unserved_nodes.map(escapeHtml).join("、")}</span>` +
        `<div class="sub">失供需水量 Σd = <b>${fmt(s.unserved_demand)}</b>（不生成水头）</div>`;
    } else if (s.status === "not_converged") {
      rowCls = "row-noconverge";
      unserved = "—";
      minHeadCell = "—";
      below = "—";
      newly = `<span class="nc-note">该情景单独不收敛，已排最后，不影响其余情景。</span>`;
    } else {
      if (s.min_demand_head < a.min_head) rowCls = "row-lowhead";
      minHeadCell = `${fmt(s.min_demand_head)}` +
        ` <span class="hint">@ ${escapeHtml(s.min_demand_head_node)}</span>`;
      below = s.below_min_head.length
        ? s.below_min_head.map(escapeHtml).join("、")
        : `<span class="pill pill-ok">无</span>`;
      newly = s.new_below_min_head.length
        ? `<span class="pill pill-low">${s.new_below_min_head.map(escapeHtml).join("、")}</span>`
        : `<span class="pill pill-ok">无新增</span>`;
    }
    return `<tr class="${rowCls}">
      <td class="num-col">${s.rank}</td>
      <td class="cell-id">${pipeDesc}</td>
      <td>${outageStatusCell(s)}</td>
      <td>${unserved}</td>
      <td class="num-col">${minHeadCell}</td>
      <td>${below}</td>
      <td>${newly}</td>
    </tr>`;
  }).join("");

  $("#outagePanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function setBanner(kind, text) {
  const b = $("#banner");
  b.className = `banner banner-${kind}`;
  b.textContent = text;
}

/* 求解后任何编辑都会让旧结果失效，提示重新求解 */
function markDirty() {
  const hadOutage = !!state.outage;
  const hadResult = !!state.result;
  clearOutage();
  if (!hadResult) {
    if (hadOutage) setBanner("idle", "输入已修改，旧的停管分析已清除，请重新点击“停管分析”。");
    return;
  }
  state.result = null;
  $("#resultsPanel").hidden = true;
  setBanner("idle", "输入已修改，请重新点击“求解”。");
}

/* 节点、管段或最低水头一旦变动，旧的停管分析立即失效并清除 */
function clearOutage() {
  if (!state.outage) return;
  state.outage = null;
  $("#outagePanel").hidden = true;
}

/* ------------------------------ 结果展示 ------------------------------ */
function renderResults(r) {
  $("#resultsPanel").hidden = false;

  const cards = [
    { k: `水源“${r.source_node}”总供水量`, v: fmt(r.source_supply), cls: "good" },
    { k: "总需水量 Σd", v: fmt(r.total_demand) },
    { k: "供耗差（应≈0）", v: fmt(r.balance_residual, 2),
      cls: Math.abs(r.balance_residual) <= r.tolerance ? "good" : "bad" },
    { k: "节点守恒最大残差", v: fmt(r.max_node_residual, 2),
      cls: r.max_node_residual <= r.tolerance ? "good" : "bad" },
    { k: "管段压降最大残差", v: fmt(r.max_pipe_residual, 2),
      cls: r.max_pipe_residual <= r.tolerance ? "good" : "bad" },
    { k: "低于最低水头节点数", v: String(r.below_min_head.length),
      cls: r.below_min_head.length ? "bad" : "good" },
  ];
  $("#summaryGrid").innerHTML = cards.map((c) =>
    `<div class="summary-card ${c.cls || ""}"><div class="k">${c.k}</div><div class="v">${c.v}</div></div>`
  ).join("");

  $("#nodeResultTbody").innerHTML = r.node_results.map((n) => {
    const typeCell = n.type === "source"
      ? `<span class="pill pill-src">水源</span>`
      : `用水`;
    let status;
    if (n.below_min_head) status = `<span class="pill pill-low">低于 ${fmt(r.min_head)}</span>`;
    else status = `<span class="pill pill-ok">合格</span>`;
    const residual = n.type === "source" ? "—（固定水头）" : fmt(n.residual, 2);
    return `<tr class="${n.below_min_head ? "row-low" : ""}">
      <td class="cell-id">${escapeHtml(n.id)}</td>
      <td>${typeCell}</td>
      <td class="num-col">${fmt(n.head)}</td>
      <td class="num-col">${n.type === "demand" ? fmt(n.demand) : "—"}</td>
      <td class="num-col">${fmt(n.net_inflow)}</td>
      <td class="num-col">${residual}</td>
      <td>${status}</td>
    </tr>`;
  }).join("");

  $("#pipeResultTbody").innerHTML = r.pipe_results.map((p) => {
    const qCls = p.flow > 0 ? "flow-pos" : p.flow < 0 ? "flow-neg" : "";
    return `<tr>
      <td class="cell-id">${escapeHtml(p.id)}</td>
      <td class="cell-id">${escapeHtml(p.start)} → ${escapeHtml(p.end)}</td>
      <td class="num-col">${fmt(p.r)}</td>
      <td class="num-col ${qCls}">${fmt(p.flow)}</td>
      <td>${escapeHtml(p.direction)}</td>
      <td class="num-col">${fmt(p.head_drop)}</td>
      <td class="num-col">${fmt(p.residual, 2)}</td>
    </tr>`;
  }).join("");

  // 让结果卡片进入视野
  $("#resultsPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------ 事件绑定 ------------------------------ */
$("#addNodeBtn").addEventListener("click", addNode);
$("#addPipeBtn").addEventListener("click", addPipe);
$("#sampleBtn").addEventListener("click", loadSample);
$("#resetBtn").addEventListener("click", resetAll);
$("#solveBtn").addEventListener("click", solve);
$("#outageBtn").addEventListener("click", analyzeOutages);
$("#relayoutBtn").addEventListener("click", () => { initPositions(true); draw(); });
$("#minHeadInput").addEventListener("input", (e) => {
  state.minHead = e.target.value;
  // 最低水头限值属于分析参数：一旦改动，旧停管分析立即清除
  clearOutage();
  if (state.result) {
    // 最低水头变化时本地重算标红，无需重新求解
    const mh = Number(state.minHead);
    if (Number.isFinite(mh)) {
      state.result.min_head = mh;
      state.result.below_min_head = state.result.node_results
        .filter((n) => n.head < mh).map((n) => n.id);
      renderResults(state.result);
      draw();
    }
  }
});

// 初始化
resetAll();
