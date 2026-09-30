"""
闭环供水管网平衡台 —— 稳态水力求解器。

数学模型（管段 p 按录入方向 i→j，流量 q_p 为带符号标量）：

    压降方程  H_i - H_j = r_p · q_p · |q_p|
    节点守恒  净流入 = 需水量 d_k            （用水节点 k）

水源节点给定固定水头 H_s，其余节点给定非负需水量 d_k；
未知量为各用水节点水头 H_k 与各管段带符号流量 q_p，方程数与未知量相等，
构成可由 Newton 法（SciPy）求解的平方非线性方程组。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any

import numpy as np
from scipy import optimize

# 对外校验容差：节点守恒与每段压降的残差必须不超过该值
CHECK_TOL = 1.0e-6
# 求解器内部目标残差（比交付校验更严，留出余量）
SOLVER_TARGET_TOL = 1.0e-9

MIN_NODES, MAX_NODES = 2, 10
MIN_PIPES, MAX_PIPES = 1, 16


class ValidationError(ValueError):
    """提交的管网描述不合法。"""


class SolverNotConverged(RuntimeError):
    """非线性方程组未能收敛到要求容差。"""


@dataclass(frozen=True)
class Network:
    nodes: list[dict[str, Any]]  # [{"id","type","head"|"demand"}]，保持录入顺序
    pipes: list[dict[str, Any]]  # [{"id","start","end","r"}]
    min_head: float


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _as_finite(value: Any, label: str, *, nonneg: bool = False,
               positive: bool = False) -> float:
    """把外部输入转成有限 float，按要求检查符号；非法时抛中文 ValidationError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}必须是有限数值。")
    f = float(value)
    if not math.isfinite(f):
        raise ValidationError(f"{label}必须是有限数值（不允许 NaN 或无穷大）。")
    if positive and f <= 0.0:
        raise ValidationError(f"{label}必须为正数，当前为 {f:g}。")
    if nonneg and f < 0.0:
        raise ValidationError(f"{label}不能为负值，当前为 {f:g}。")
    return f


def validate_payload(payload: Any) -> Network:
    """完整校验 API 输入，返回规范化 Network；任何非法情形抛 ValidationError。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是包含 nodes、pipes 的对象。")

    raw_nodes = payload.get("nodes")
    raw_pipes = payload.get("pipes")
    if not isinstance(raw_nodes, list):
        raise ValidationError("nodes 必须是节点对象数组。")
    if not isinstance(raw_pipes, list):
        raise ValidationError("pipes 必须是管段对象数组。")

    if not (MIN_NODES <= len(raw_nodes) <= MAX_NODES):
        raise ValidationError(
            f"节点数必须在 {MIN_NODES}～{MAX_NODES} 之间，当前为 {len(raw_nodes)} 个。")
    if not (MIN_PIPES <= len(raw_pipes) <= MAX_PIPES):
        raise ValidationError(
            f"管段数必须在 {MIN_PIPES}～{MAX_PIPES} 之间，当前为 {len(raw_pipes)} 根。")

    # ---- 节点 -------------------------------------------------------------
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    source_count = 0
    for k, raw in enumerate(raw_nodes, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {k} 个节点必须是对象。")
        nid = raw.get("id")
        if not isinstance(nid, str) or not nid.strip():
            raise ValidationError(f"第 {k} 个节点缺少有效的节点编号（非空字符串）。")
        nid = nid.strip()
        if nid in seen_ids:
            raise ValidationError(f"节点编号“{nid}”重复，节点编号必须唯一。")
        seen_ids.add(nid)

        ntype = raw.get("type")
        if ntype not in ("source", "demand"):
            raise ValidationError(
                f"节点“{nid}”的类型必须是 source（水源）或 demand（用水节点）。")

        if ntype == "source":
            head = _as_finite(raw.get("head"), f"水源节点“{nid}”的固定水头")
            nodes.append({"id": nid, "type": "source", "head": head})
            source_count += 1
        else:
            demand = _as_finite(
                raw.get("demand"), f"用水节点“{nid}”的需水量", nonneg=True)
            nodes.append({"id": nid, "type": "demand", "demand": demand})

    if source_count == 0:
        raise ValidationError("必须恰好指定 1 个水源节点（固定水头），当前为 0 个。")
    if source_count > 1:
        raise ValidationError(
            f"必须恰好指定 1 个水源节点，当前存在 {source_count} 个。")

    min_head = _as_finite(payload.get("min_head"), "最低水头限值")

    # ---- 管段 -------------------------------------------------------------
    pipes: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    seen_pipe_ids: set[str] = set()
    for k, raw in enumerate(raw_pipes, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {k} 根管段必须是对象。")
        pid = raw.get("id", f"P{k}")
        if not isinstance(pid, str) or not pid.strip():
            raise ValidationError(f"第 {k} 根管段的编号必须是非空字符串。")
        pid = pid.strip()
        if pid in seen_pipe_ids:
            raise ValidationError(f"管段编号“{pid}”重复，管段编号必须唯一。")
        seen_pipe_ids.add(pid)

        start = raw.get("start")
        end = raw.get("end")
        if not isinstance(start, str) or not start.strip():
            raise ValidationError(f"管段“{pid}”缺少起点编号。")
        start = start.strip()
        if not isinstance(end, str) or not end.strip():
            raise ValidationError(f"管段“{pid}”缺少终点编号。")
        end = end.strip()
        if start not in seen_ids:
            raise ValidationError(f"管段“{pid}”的起点“{start}”不是已定义的节点。")
        if end not in seen_ids:
            raise ValidationError(f"管段“{pid}”的终点“{end}”不是已定义的节点。")
        if start == end:
            raise ValidationError(
                f"管段“{pid}”的起点与终点都是“{start}”，不允许自环管段。")

        edge_key = tuple(sorted((start, end)))
        if edge_key in seen_edges:
            raise ValidationError(
                f"管段“{pid}”的端点对 {start}–{end} 与已有管段重复，"
                f"同一对节点之间只允许一根管段。")
        seen_edges.add(edge_key)

        resistance = _as_finite(raw.get("r"), f"管段“{pid}”的阻力 r", positive=True)
        pipes.append({"id": pid, "start": start, "end": end, "r": resistance})

    # ---- 无向连通性（并查集） ---------------------------------------------
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pipe in pipes:
        a, b = find(pipe["start"]), find(pipe["end"])
        if a != b:
            parent[a] = b

    root = find(nodes[0]["id"])
    unreachable = [n["id"] for n in nodes if find(n["id"]) != root]
    if unreachable:
        raise ValidationError(
            "管网必须是无向连通图，以下节点无法经由管段与其他节点连通："
            + "、".join(unreachable) + "。")

    return Network(nodes=nodes, pipes=pipes, min_head=min_head)


# --------------------------------------------------------------------------- #
# 稳态求解
# --------------------------------------------------------------------------- #
def _bfs_tree_initial(net: Network, index: dict[str, int], source: int,
                      demands: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """以水源为根做 BFS 生成树：用子树需水量摊派初始流量，沿程估算初始水头。

    返回 (全节点初始水头, 全部管段初始带符号流量)。
    """
    n = len(net.nodes)
    m = len(net.pipes)
    adj: list[list[tuple[int, int, float]]] = [[] for _ in range(n)]
    for p, pipe in enumerate(net.pipes):
        u, v = index[pipe["start"]], index[pipe["end"]]
        adj[u].append((v, p, 1.0))   # 录入方向 u->v 时 A[u,p]=+1
        adj[v].append((u, p, -1.0))  # 反向遍历时 q 系数为 -1

    par = [-1] * n
    pedge = [-1] * n
    psign = [0.0] * n
    order: list[int] = []
    seen = [False] * n
    seen[source] = True
    queue = deque([source])
    while queue:
        u = queue.popleft()
        order.append(u)
        for v, p, s in adj[u]:
            if not seen[v]:
                seen[v] = True
                par[v], pedge[v], psign[v] = u, p, s
                queue.append(v)

    q0 = np.zeros(m)
    subtree = demands.copy()
    with np.errstate(all="ignore"):
        for v in reversed(order):
            if v == source:
                continue
            # 水由父节点流向子节点；管段录入方向与父->子一致 (s=+1) 时 q>0
            q0[pedge[v]] += -psign[v] * subtree[v]
            subtree[par[v]] += subtree[v]
        # 循环结束后 subtree[v] 即边 par[v]→v 承担的子树总需水量

        h0 = np.zeros(n)
        h0[source] = net.nodes[source]["head"]
        for v in order:
            if v == source:
                continue
            r_e = net.pipes[pedge[v]]["r"]
            h0[v] = h0[par[v]] - r_e * subtree[v] * abs(subtree[v])

    return h0, q0


def _newton_backtrack(fun, jac, x0: np.ndarray, *, max_iter: int = 300,
                      tol: float = SOLVER_TARGET_TOL):
    """带 Armijo 回退的全量 Newton 法，作为 SciPy 求解器之外的保险策略。"""
    x = x0.astype(float).copy()
    f = fun(x)
    norm = float(np.max(np.abs(f))) if np.all(np.isfinite(f)) else math.inf
    iterations = 0
    for iterations in range(1, max_iter + 1):
        if norm <= tol:
            return x, iterations, norm
        try:
            delta = np.linalg.solve(jac(x), -f)
        except np.linalg.LinAlgError:
            return x, iterations, norm
        if not np.all(np.isfinite(delta)):
            return x, iterations, norm
        step = 1.0
        accepted = False
        for _ in range(60):
            xn = x + step * delta
            fn = fun(xn)
            if np.all(np.isfinite(fn)):
                nn = float(np.max(np.abs(fn)))
                if nn < (1.0 - 1.0e-4 * step) * norm:
                    x, f, norm = xn, fn, nn
                    accepted = True
                    break
            step *= 0.5
        if not accepted:
            return x, iterations, norm
    return x, iterations, norm


def solve_network(net: Network) -> dict[str, Any]:
    """求稳态解并按 1e-6 容差复核；不收敛时抛 SolverNotConverged。"""
    n = len(net.nodes)
    m = len(net.pipes)
    ids = [node["id"] for node in net.nodes]
    index = {nid: i for i, nid in enumerate(ids)}
    source = next(i for i, node in enumerate(net.nodes) if node["type"] == "source")
    demand_positions = [i for i in range(n) if i != source]
    n_dem = n - 1

    fixed_head = np.zeros(n)
    demands = np.zeros(n)
    for i, node in enumerate(net.nodes):
        if node["type"] == "source":
            fixed_head[i] = node["head"]
        else:
            demands[i] = node["demand"]
    h_source = fixed_head[source]
    d = demands[demand_positions]

    # 节点-管段关联矩阵 A（按录入方向：起点 +1，终点 -1），故 A·q 为净流出
    A = np.zeros((n, m))
    r = np.zeros(m)
    for p, pipe in enumerate(net.pipes):
        A[index[pipe["start"]], p] = 1.0
        A[index[pipe["end"]], p] = -1.0
        r[p] = pipe["r"]
    Ad = A[demand_positions, :]

    def residual(x: np.ndarray) -> np.ndarray:
        h_dem, q = x[:n_dem], x[n_dem:]
        h = fixed_head.copy()
        h[demand_positions] = h_dem
        f = np.empty(n_dem + m)
        # 用水节点：净流出 = -需水量，即净流入 = 需水量
        with np.errstate(all="ignore"):
            f[:n_dem] = Ad @ q + d
            f[n_dem:] = A.T @ h - r * q * np.abs(q)  # H起-H终 = r·q·|q|
        return f

    def jacobian(x: np.ndarray) -> np.ndarray:
        q = x[n_dem:]
        J = np.zeros((n_dem + m, n_dem + m))
        J[:n_dem, n_dem:] = Ad
        J[n_dem:, :n_dem] = Ad.T
        with np.errstate(all="ignore"):
            np.fill_diagonal(J[n_dem:, n_dem:], -2.0 * r * np.abs(q))
        return J

    # ---- 初始点：BFS 摊派 + 若干平坦兜底 ---------------------------------
    h_tree, q_tree = _bfs_tree_initial(net, index, source, demands)
    starts: list[np.ndarray] = [
        np.concatenate([h_tree[demand_positions], q_tree]),
        np.concatenate([np.full(n_dem, h_source), np.zeros(m)]),
        np.concatenate([np.full(n_dem, h_source - 1.0), np.zeros(m)]),
    ]

    def residual_norm(xx: np.ndarray) -> float:
        ff = residual(xx)
        return float(np.max(np.abs(ff))) if np.all(np.isfinite(ff)) else math.inf

    best_x: np.ndarray | None = None
    best_norm = math.inf
    best_method = ""
    best_iters = 0

    for x0 in starts:
        for method, options in (
            ("hybr", {"col_deriv": True, "xtol": 1.0e-12, "maxfev": 4000}),
            ("lm", {"col_deriv": True, "xtol": 1.0e-12, "maxiter": 2000,
                    "factor": 0.1}),
        ):
            try:
                sol = optimize.root(
                    residual, x0, jac=jacobian, method=method, options=options)
                x_try = np.asarray(sol.x, dtype=float)
                nrm = residual_norm(x_try) if np.all(np.isfinite(x_try)) else math.inf
                iters = int(getattr(sol, "njev", None) or getattr(sol, "nfev", 0) or 0)
            except Exception:  # noqa: BLE001 - 任何数值异常都转由兜底策略处理
                nrm, x_try, iters = math.inf, x0, 0
            if nrm < best_norm:
                best_norm, best_x, best_method, best_iters = nrm, x_try, method, iters
        x_bt, iters_bt, nrm_bt = _newton_backtrack(residual, jacobian, x0)
        if nrm_bt < best_norm:
            best_norm, best_x, best_method, best_iters = (
                nrm_bt, x_bt, "newton-backtrack", iters_bt)
        if best_norm <= SOLVER_TARGET_TOL:
            break

    if best_x is None or best_norm > SOLVER_TARGET_TOL:
        raise SolverNotConverged(
            "非线性方程组未能收敛到 1e-9 内部容差"
            f"（最佳残差 ‖f‖∞ = {best_norm:.3e}）。"
            "请检查阻力与需水量的数量级是否过于悬殊后重试。")

    # ---- 组装结果并独立复核 ----------------------------------------------
    h = fixed_head.copy()
    h[demand_positions] = best_x[:n_dem]
    q = best_x[n_dem:].copy()
    q[q == 0.0] = 0.0  # 归一化 -0.0，便于界面显示
    net_inflow = -A @ q  # A·q 为净流出，取负即净流入

    pipe_results: list[dict[str, Any]] = []
    max_pipe_res = 0.0
    for p, pipe in enumerate(net.pipes):
        u, v = index[pipe["start"]], index[pipe["end"]]
        qp = float(q[p])
        dh = float(h[u] - h[v])
        pipe_res = dh - r[p] * qp * abs(qp)
        max_pipe_res = max(max_pipe_res, abs(pipe_res))
        if qp > 0.0:
            direction = f"{pipe['start']} → {pipe['end']}"
        elif qp < 0.0:
            direction = f"{pipe['end']} → {pipe['start']}"
        else:
            direction = "零流量（无流向）"
        pipe_results.append({
            "id": pipe["id"],
            "start": pipe["start"],
            "end": pipe["end"],
            "r": float(r[p]),
            "flow": qp,
            "direction": direction,
            "head_start": float(h[u]),
            "head_end": float(h[v]),
            "head_drop": dh,
            "head_loss": abs(dh),
            "residual": float(pipe_res),
        })

    node_results: list[dict[str, Any]] = []
    max_node_res = 0.0
    below: list[str] = []
    for i, node in enumerate(net.nodes):
        row: dict[str, Any] = {
            "id": node["id"],
            "type": node["type"],
            "head": float(h[i]),
            "net_inflow": float(net_inflow[i]),
            "below_min_head": bool(h[i] < net.min_head),
        }
        if h[i] < net.min_head:
            below.append(node["id"])
        if node["type"] == "source":
            row["fixed_head"] = node["head"]
            row["outflow"] = float(-net_inflow[i])  # 水源经管道净流出 = 供水量（正值）
        else:
            row["demand"] = node["demand"]
            node_res = float(net_inflow[i] - demands[i])
            row["residual"] = node_res
            max_node_res = max(max_node_res, abs(node_res))
        node_results.append(row)

    total_demand = float(np.sum(demands))
    source_supply = float(-net_inflow[source])  # 水源净流出 = 总供水量
    balance_gap = source_supply - total_demand

    checks_ok = (
        max_node_res <= CHECK_TOL
        and max_pipe_res <= CHECK_TOL
        and abs(balance_gap) <= CHECK_TOL
    )
    if not checks_ok:  # 理论上不会发生：求解器内部容差更严
        raise SolverNotConverged(
            "解虽已求出，但按 1e-6 容差复核未通过："
            f"节点最大残差 {max_node_res:.3e}，管段最大残差 {max_pipe_res:.3e}，"
            f"供耗差 {balance_gap:.3e}。")

    return {
        "converged": True,
        "tolerance": CHECK_TOL,
        "method": best_method,
        "iterations": best_iters,
        "solver_residual_inf": best_norm,
        "node_results": node_results,
        "pipe_results": pipe_results,
        "heads": {node["id"]: float(h[index[node["id"]]]) for node in net.nodes},
        "flows": {pipe["id"]: float(q[p]) for p, pipe in enumerate(net.pipes)},
        "source_node": net.nodes[source]["id"],
        "source_supply": source_supply,
        "total_demand": total_demand,
        "balance_residual": balance_gap,
        "max_node_residual": max_node_res,
        "max_pipe_residual": max_pipe_res,
        "min_head": net.min_head,
        "below_min_head": below,
    }


# --------------------------------------------------------------------------- #
# 逐根停管情景分析
# --------------------------------------------------------------------------- #
def _reachable_nodes(net: Network, disabled: int) -> set[str]:
    """无向 BFS：返回不使用第 disabled 根管段时，从水源可达的全部节点编号。"""
    source_id = next(n["id"] for n in net.nodes if n["type"] == "source")
    adj: dict[str, list[str]] = {n["id"]: [] for n in net.nodes}
    for p, pipe in enumerate(net.pipes):
        if p == disabled:
            continue
        adj[pipe["start"]].append(pipe["end"])
        adj[pipe["end"]].append(pipe["start"])
    seen = {source_id}
    queue = deque([source_id])
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                queue.append(v)
    return seen


def analyze_pipe_outages(net: Network) -> dict[str, Any]:
    """对每根管段各构造一个“移除该管”的独立情景并评估后果。

    情景分为三类：
      * disconnected —— 有用水节点与水源断开，列出失供节点与失供需水量之和，
        不生成水头；
      * connected    —— 全部节点仍与水源连通，重新稳态求解，列出最低用水节点
        水头、低于当前限值的节点及相对基线新增的低水头节点；
      * not_converged —— 该情景单独不收敛，标明后排在最后，不影响其余情景。

    排序：断联情景按失供量降序在前，连通情景按最低用水节点水头升序随后，
    同分保持管段录入顺序；不收敛情景一律排最后（同分同样保持录入顺序）。
    """
    baseline = solve_network(net)
    demand_map = {n["id"]: n["demand"] for n in net.nodes if n["type"] == "demand"}
    baseline_below = set(baseline["below_min_head"])

    scenarios: list[dict[str, Any]] = []
    for p, pipe in enumerate(net.pipes):
        scenario: dict[str, Any] = {
            "rank": 0,
            "pipe_id": pipe["id"],
            "pipe_index": p,
            "removed_pipe": {"id": pipe["id"], "start": pipe["start"],
                             "end": pipe["end"], "r": pipe["r"]},
        }
        reachable = _reachable_nodes(net, p)
        unserved = [nid for nid in demand_map if nid not in reachable]
        if unserved:
            scenario.update({
                "status": "disconnected",
                "unserved_nodes": unserved,
                "unserved_demand": float(sum(demand_map[nid] for nid in unserved)),
            })
            scenarios.append(scenario)
            continue

        # 仍连通：在剔除该管段的子网上重新求解
        sub_net = Network(
            nodes=net.nodes,
            pipes=net.pipes[:p] + net.pipes[p + 1:],
            min_head=net.min_head,
        )
        try:
            result = solve_network(sub_net)
        except SolverNotConverged as exc:
            scenario.update({"status": "not_converged", "error": str(exc)})
            scenarios.append(scenario)
            continue

        heads = result["heads"]
        below = result["below_min_head"]
        demand_ids = list(demand_map)
        scenario.update({
            "status": "connected",
            "min_demand_head": float(min(heads[nid] for nid in demand_ids)),
            # 水头并列时 min 取录入顺序的第一个节点
            "min_demand_head_node": min(demand_ids, key=lambda nid: heads[nid]),
            "below_min_head": below,
            "new_below_min_head": [nid for nid in below if nid not in baseline_below],
            "source_supply": result["source_supply"],
            "total_demand": result["total_demand"],
            "solution": result,
        })
        scenarios.append(scenario)

    def sort_key(s: dict[str, Any]) -> tuple[int, float, int]:
        if s["status"] == "disconnected":
            # 失供量越大越靠前：取负实现降序
            return (0, -s["unserved_demand"], s["pipe_index"])
        if s["status"] == "connected":
            return (1, s["min_demand_head"], s["pipe_index"])
        return (2, 0.0, s["pipe_index"])

    scenarios.sort(key=sort_key)
    for rank, s in enumerate(scenarios, start=1):
        s["rank"] = rank

    counts = {"disconnected": 0, "connected": 0, "not_converged": 0}
    for s in scenarios:
        counts[s["status"]] += 1

    return {
        "min_head": net.min_head,
        "baseline": baseline,
        "baseline_below_min_head": baseline["below_min_head"],
        "scenarios": scenarios,
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# 基于实测水头的单点漏损定位
# --------------------------------------------------------------------------- #
MIN_MEASUREMENTS, MAX_MEASUREMENTS = 2, 8
# 候选间“无法区分”的 RMSE 阈值：与最优 RMSE 相差不超过该值即视为同分
LEAK_RMSE_TIE_TOL = 1.0e-4
# 黄金分割搜索迭代次数（每迭代区间按 0.618 缩减，40 次后相对宽度约 6.6e-9）
LEAK_GOLDEN_ITERATIONS = 40
# 粗扫网格的区间数（共 N+1 个均匀点，含两个端点），先定位全局最优所在网格
LEAK_GRID_SECTIONS = 16
# 最优漏量相对上限小到该比例以内时，按数值噪声归并为端点 0（无漏损）
LEAK_ZERO_REL = 1.0e-9


@dataclass(frozen=True)
class LeakCase:
    """一次单点漏损定位的全部输入。"""
    network: Network
    measurements: list[tuple[str, float]]  # [(测点编号, 实测水头)]，保持录入顺序
    leak_upper_bound: float


def validate_leak_payload(payload: Any) -> LeakCase:
    """在校验管网的基础上追加校验测点与漏量上限，返回规范化 LeakCase。"""
    net = validate_payload(payload)
    demand_ids = {n["id"] for n in net.nodes if n["type"] == "demand"}

    raw_measurements = payload.get("measurements")
    if not isinstance(raw_measurements, list):
        raise ValidationError("measurements 必须是测点对象数组。")
    if not (MIN_MEASUREMENTS <= len(raw_measurements) <= MAX_MEASUREMENTS):
        raise ValidationError(
            f"实测水头测点必须在 {MIN_MEASUREMENTS}～{MAX_MEASUREMENTS} 个之间，"
            f"当前为 {len(raw_measurements)} 个。")

    measurements: list[tuple[str, float]] = []
    seen_measurements: set[str] = set()
    for k, raw in enumerate(raw_measurements, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {k} 个测点必须是对象。")
        nid = raw.get("node")
        if not isinstance(nid, str) or not nid.strip():
            raise ValidationError(f"第 {k} 个测点缺少有效的节点编号（非空字符串）。")
        nid = nid.strip()
        if nid not in demand_ids:
            raise ValidationError(
                f"测点“{nid}”不是已定义的用水节点（测点必须全部为用水节点）。")
        if nid in seen_measurements:
            raise ValidationError(f"测点“{nid}”重复，所有测点必须互异。")
        seen_measurements.add(nid)
        head = _as_finite(raw.get("head"), f"测点“{nid}”的实测水头")
        measurements.append((nid, head))

    upper = _as_finite(
        payload.get("leak_upper_bound"), "漏量上限 leak_upper_bound", positive=True)

    return LeakCase(network=net, measurements=measurements, leak_upper_bound=upper)


def _network_with_extra_demand(net: Network, leak_node_id: str,
                               extra: float) -> Network:
    """在指定用水节点的需水量上增加 extra（其余数据不变），返回新 Network。"""
    nodes: list[dict[str, Any]] = []
    for node in net.nodes:
        if node["type"] == "demand" and node["id"] == leak_node_id:
            bumped = dict(node)
            bumped["demand"] = float(node["demand"]) + float(extra)
            nodes.append(bumped)
        else:
            nodes.append(node)
    return Network(nodes=nodes, pipes=net.pipes, min_head=net.min_head)


def locate_single_leak(case: LeakCase) -> dict[str, Any]:
    """假定至多一个用水节点存在漏损，逐节点在 [0, 上限] 内拟合最优漏量。

    流程：
      1. 求无漏损基线（不收敛则直接抛 SolverNotConverged），计算基线测点 RMSE；
      2. 对每个用水节点，把“在该节点增加漏量 x”当作一维问题：测点预测水头由
         稳态求解器给出，目标为测点均方根误差 RMSE(x)。额外取水使各点水头沿 x
         单调不升，RMSE 为分段光滑曲线；为兼顾多峰/平底情形，先在 [0, 上限] 上
         均匀粗扫（两端点必评）锁定全局最优所在网格，再在该网格内黄金分割精修，
         端点 0 与上限都显式参与比较，不收敛的点按 +∞ 处理；
      3. 单个候选拟合失败（上限端无法求解等）时标明 not_converged 并排最后，
         不影响其他候选；
      4. 成功候选按 RMSE 升序、同分按节点录入顺序排列；与最优 RMSE 相差不超过
         1e-4 的候选至少有两个时，标记为“无法区分”；最优漏量为 0 时不判定漏损。
    """
    net = case.network
    upper = float(case.leak_upper_bound)
    meas_ids = [nid for nid, _ in case.measurements]
    meas_heads = np.array([h for _, h in case.measurements], dtype=float)
    n_meas = len(meas_ids)

    # ---- 无漏损基线（失败则整个分析无法进行） -----------------------------
    baseline = solve_network(net)
    baseline_pred = {nid: float(baseline["heads"][nid]) for nid in meas_ids}
    baseline_vec = np.array([baseline_pred[nid] for nid in meas_ids])
    baseline_sse = float(np.sum((baseline_vec - meas_heads) ** 2))
    baseline_rmse = math.sqrt(baseline_sse / n_meas)

    candidates: list[dict[str, Any]] = []
    for node_index, node in enumerate(net.nodes):
        if node["type"] != "demand":
            continue
        cand_id = node["id"]
        candidate: dict[str, Any] = {"node_id": cand_id, "node_index": node_index}
        try:
            # x -> (sse, {测点: 预测水头})；None 表示该漏量下稳态求解失败。
            # 所有候选的 x=0 都是同一条无漏损基线，直接播种，避免重复求解。
            cache: dict[float, tuple[float, dict[str, float]] | None] = {
                0.0: (baseline_sse, baseline_pred)}

            def evaluate(x: float):
                x = float(x)
                if x in cache:
                    return cache[x]
                try:
                    result = solve_network(_network_with_extra_demand(net, cand_id, x))
                except SolverNotConverged:
                    cache[x] = None
                    return None
                pred = {nid: float(result["heads"][nid]) for nid in meas_ids}
                vec = np.array([pred[nid] for nid in meas_ids])
                sse = float(np.sum((vec - meas_heads) ** 2))
                cache[x] = (sse, pred)
                return cache[x]

            # 上端点必须可比较；上限端不收敛则该候选拟合失败
            if evaluate(upper) is None:
                raise SolverNotConverged(
                    f"漏量上限 {upper:g} 处稳态求解未能收敛，无法完成上端点比较。")

            # ---- 均匀粗扫（两端点必评）：定位全局最优所在网格 ----------------
            grid = [upper * i / LEAK_GRID_SECTIONS
                    for i in range(LEAK_GRID_SECTIONS + 1)]
            grid_vals: list[float] = []
            for x in grid:
                value = evaluate(x)
                grid_vals.append(math.inf if value is None else value[0])
            best_i = min(range(len(grid)), key=lambda i: (grid_vals[i], i))
            lo_i = best_i - 1 if best_i > 0 else 0
            hi_i = best_i + 1 if best_i < LEAK_GRID_SECTIONS else LEAK_GRID_SECTIONS

            # ---- 在包围最优网格点的区间内黄金分割精修 ----------------------
            a, b = grid[lo_i], grid[hi_i]
            golden = (math.sqrt(5.0) - 1.0) / 2.0
            x1, x2 = b - golden * (b - a), a + golden * (b - a)
            f1, f2 = evaluate(x1), evaluate(x2)
            for _ in range(LEAK_GOLDEN_ITERATIONS):
                v1 = math.inf if f1 is None else f1[0]
                v2 = math.inf if f2 is None else f2[0]
                if a == b:  # 最优恰在端点（区间宽度为 0）时无需精修
                    break
                if v1 < v2:
                    b, x2, f2 = x2, x1, f1
                    x1 = b - golden * (b - a)
                    f1 = evaluate(x1)
                else:
                    a, x1, f1 = x1, x2, f2
                    x2 = a + golden * (b - a)
                    f2 = evaluate(x2)

            # 在所有实际评估过的漏量中取最优；同分优先较小漏量（含端点 0）
            best_x, (best_sse, best_pred) = min(
                ((x, value) for x, value in cache.items() if value is not None),
                key=lambda item: (item[1][0], item[0]))

            # 最优位置与 0 仅差数值噪声时，归并为无漏损端点
            if 0.0 < float(best_x) <= LEAK_ZERO_REL * upper:
                best_x, best_sse, best_pred = (
                    0.0, baseline_sse, baseline_pred)

            candidate.update({
                "status": "fitted",
                "leak": float(best_x),
                "predicted_heads": best_pred,
                "head_errors": {nid: float(best_pred[nid] - h)
                                for nid, h in zip(meas_ids, meas_heads)},
                "sse": float(best_sse),
                "rmse": math.sqrt(best_sse / n_meas),
                "evaluations": len(cache),
            })
        except Exception as exc:  # noqa: BLE001 - 单候选失败隔离，不影响其余候选
            candidate.update({"status": "not_converged", "error": str(exc)})
        candidates.append(candidate)

    # 排序：成功候选按 RMSE 升序，同分按节点录入顺序；失败候选一律排最后
    def candidate_key(c: dict[str, Any]) -> tuple[int, float, int]:
        if c["status"] == "fitted":
            return (0, c["rmse"], c["node_index"])
        return (1, 0.0, c["node_index"])

    candidates.sort(key=candidate_key)
    for rank, c in enumerate(candidates, start=1):
        c["rank"] = rank

    fitted = [c for c in candidates if c["status"] == "fitted"]
    counts = {"fitted": len(fitted),
              "not_converged": len(candidates) - len(fitted)}

    best = fitted[0] if fitted else None
    indistinguishable_nodes: list[str] = []
    if best is not None:
        best_rmse = best["rmse"]
        indistinguishable_nodes = [
            c["node_id"] for c in fitted
            if c["rmse"] - best_rmse <= LEAK_RMSE_TIE_TOL]
    indistinguishable = len(indistinguishable_nodes) >= 2
    leak_detected = best is not None and best["leak"] > 0.0

    if best is None:
        conclusion = "所有候选节点的漏损拟合均失败，无法判断漏损位置。"
        leak_node = None
    elif not leak_detected:
        leak_node = None
        if indistinguishable:
            conclusion = (
                "最优漏量为 0，按规则不判定存在漏损；但有 "
                f"{len(indistinguishable_nodes)} 个候选（"
                + "、".join(indistinguishable_nodes)
                + "）的测点误差与最优值相差不超过 1e-4，彼此无法区分。")
        else:
            conclusion = "最优漏量为 0，实测水头与无漏损基线吻合，按规则不判定存在漏损。"
    else:
        leak_node = best["node_id"]
        if indistinguishable:
            conclusion = (
                f"测点误差最小的节点为“{leak_node}”，拟合漏量 "
                f"{best['leak']:.6g}；但有 {len(indistinguishable_nodes)} 个候选（"
                + "、".join(indistinguishable_nodes)
                + "）的误差与最优值相差不超过 1e-4，漏损位置无法区分。")
        else:
            conclusion = (
                f"最可能的单点漏损位于用水节点“{leak_node}”，"
                f"拟合漏量为 {best['leak']:.6g}（测点 RMSE={best['rmse']:.6g}）。")

    return {
        "measurements": [{"node": nid, "measured_head": float(h)}
                         for nid, h in case.measurements],
        "leak_upper_bound": upper,
        "tie_tolerance": LEAK_RMSE_TIE_TOL,
        "golden_iterations": LEAK_GOLDEN_ITERATIONS,
        "baseline": baseline,
        "baseline_predicted_heads": baseline_pred,
        "baseline_rmse": baseline_rmse,
        "candidates": candidates,
        "counts": counts,
        "best": None if best is None else {
            "node_id": best["node_id"],
            "leak": best["leak"],
            "rmse": best["rmse"],
        },
        "indistinguishable": indistinguishable,
        "indistinguishable_nodes": indistinguishable_nodes,
        "leak_detected": leak_detected,
        "leak_node": leak_node,
        "conclusion": conclusion,
    }


# --------------------------------------------------------------------------- #
# 停供节点同时复供组合优选
# --------------------------------------------------------------------------- #
MIN_RESUPPLY_NODES, MAX_RESUPPLY_NODES = 2, 6
# 恢复总量“相同”的归并容差：差值不超过该绝对容差即视为同分，转入节点数/字典序比较
RESUPPLY_TOTAL_TIE_TOL = 1.0e-9


@dataclass(frozen=True)
class ResupplyCase:
    """一次停供节点复供优选的全部输入。"""
    network: Network
    # 候选停供节点编号（表内需水量为正的用水节点），按节点录入顺序
    resupply_nodes: list[str]


def validate_resupply_payload(payload: Any) -> ResupplyCase:
    """在管网校验的基础上追加校验复供候选节点，返回规范化 ResupplyCase。"""
    net = validate_payload(payload)
    demand_map = {n["id"]: n for n in net.nodes if n["type"] == "demand"}

    raw_nodes = payload.get("resupply_nodes")
    if not isinstance(raw_nodes, list):
        raise ValidationError("resupply_nodes 必须是候选停供节点编号数组。")
    if not (MIN_RESUPPLY_NODES <= len(raw_nodes) <= MAX_RESUPPLY_NODES):
        raise ValidationError(
            f"候选停供节点必须在 {MIN_RESUPPLY_NODES}～{MAX_RESUPPLY_NODES} 个之间，"
            f"当前为 {len(raw_nodes)} 个。")

    seen: set[str] = set()
    for k, raw in enumerate(raw_nodes, start=1):
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError(f"第 {k} 个候选停供节点缺少有效的节点编号（非空字符串）。")
        nid = raw.strip()
        if nid not in demand_map:
            raise ValidationError(
                f"候选停供节点“{nid}”不是已定义的用水节点"
                "（候选必须全部为用水节点，且不能是水源）。")
        if demand_map[nid]["demand"] <= 0.0:
            raise ValidationError(
                f"候选停供节点“{nid}”的表内需水量必须为正数，当前为 "
                f"{demand_map[nid]['demand']:g}。")
        if nid in seen:
            raise ValidationError(
                f"候选停供节点“{nid}”重复，候选节点必须互异。")
        seen.add(nid)

    # 候选在组合枚举中的顺序一律按节点录入顺序，而非请求数组顺序
    entry_order = [n["id"] for n in net.nodes if n["id"] in seen]
    return ResupplyCase(network=net, resupply_nodes=entry_order)


def _network_for_combo(net: Network, candidate_set: set[str],
                       restored: set[str]) -> Network:
    """构造某一复供组合对应的情景网络。

    候选节点（candidate_set 内）：在 restored 中则恢复表内需水量，否则置零；
    其他节点（含零需水用水节点与水源）：保持原样。
    """
    nodes: list[dict[str, Any]] = []
    for node in net.nodes:
        if node["type"] == "demand" and node["id"] in candidate_set \
                and node["id"] not in restored:
            stopped = dict(node)
            stopped["demand"] = 0.0
            nodes.append(stopped)
        else:
            nodes.append(node)
    return Network(nodes=nodes, pipes=net.pipes, min_head=net.min_head)


def _combo_solution_record(mask: int, candidates: list[str],
                           table_demands: dict[str, float],
                           result: dict[str, Any]) -> dict[str, Any]:
    """把一次成功求解的组合结果组装成对外的情景记录。"""
    ids = [candidates[b] for b in range(len(candidates)) if mask & (1 << b)]
    restored_total = float(sum(table_demands[nid] for nid in ids))
    # node_results 保持录入顺序，用水节点最小水头并列时取录入顺序最早者
    demand_rows = [n for n in result["node_results"] if n["type"] == "demand"]
    min_node = min(demand_rows, key=lambda row: row["head"])
    below = [n["id"] for n in demand_rows if n["below_min_head"]]
    feasible = not below
    return {
        "status": "feasible" if feasible else "below_limit",
        "nodes": ids,
        "node_count": len(ids),
        "restored_demand": restored_total,
        "min_demand_head": float(min_node["head"]),
        "min_demand_head_node": min_node["id"],
        "below_min_head": below,
        "solution": result,
    }


def plan_resupply(case: ResupplyCase) -> dict[str, Any]:
    """在现有管网上选择一组可同时复供的停供节点。

    候选节点在复供前一律按零需水计算（基线 = 空集情景），选中后才恢复表内需水量，
    其他节点不变。对包含空集在内的全部 2^k 个组合逐一做稳态求解：

      * 基线（空集）不收敛            -> status=baseline_not_converged，无法确认最优；
      * 基线收敛但有用水节点水头低于限值 -> status=baseline_below_limit，无法复供；
      * 其余任一组合不收敛             -> status=not_confirmed，无法确认最优
                                         （求解失败绝不按欠压处理），同时给出当前
                                         已确认可行组合中的临时最优；
      * 全部组合完成求解               -> status=optimal，返回可行组合中恢复需水量
                                         总和最大者；总量相同先取节点更多者，再按
                                         节点录入顺序的字典序取。
    """
    net = case.network
    candidates = list(case.resupply_nodes)
    k = len(candidates)
    cand_set = set(candidates)
    table_demands = {n["id"]: float(n["demand"])
                     for n in net.nodes if n["id"] in cand_set}
    demand_ids_in_order = [n["id"] for n in net.nodes if n["type"] == "demand"]

    def demand_heads_ok(result: dict[str, Any]) -> list[str]:
        """返回该解中水头低于限值的用水节点（按录入顺序），空列表即全部合格。"""
        return [nid for nid in demand_ids_in_order
                if result["heads"][nid] < net.min_head]

    # ---- 第 1 步：复供前基线（空集：所有候选按零需水） ---------------------
    baseline_net = _network_for_combo(net, cand_set, set())
    try:
        baseline = solve_network(baseline_net)
    except SolverNotConverged as exc:
        return {
            "status": "baseline_not_converged",
            "optimal_confirmed": False,
            "min_head": net.min_head,
            "resupply_nodes": candidates,
            "table_demands": {nid: table_demands[nid] for nid in candidates},
            "table_demand_total": float(sum(table_demands.values())),
            "evaluated_combinations": 0,
            "total_combinations": 1 << k,
            "baseline": None,
            "baseline_error": str(exc),
            "baseline_below_min_head": [],
            "combinations": [],
            "counts": {"feasible": 0, "below_limit": 0, "not_converged": 0},
            "selected": None,
            "selected_provisional": False,
            "restored_demand_total": None,
            "unrestored_nodes": list(candidates),
            "failed_combinations": [{
                "status": "not_converged",
                "nodes": [],
                "node_count": 0,
                "restored_demand": 0.0,
                "error": str(exc),
            }],
            "conclusion": (
                "复供前基线（全部候选停供、按零需水计算）的稳态求解未能收敛，"
                "无法确认最优复供组合（不能把求解失败当作欠压）。"),
        }

    baseline_below = demand_heads_ok(baseline)
    base_record = _combo_solution_record(0, candidates, table_demands, baseline)
    # 基线（空集）必然连通，head 限值判定以全部用水节点为准
    if baseline_below:
        base_record["status"] = "below_limit"
        base_record["below_min_head"] = baseline_below
        base_record["rank"] = 1
        conclusion = (
            "复供前基线（候选停供节点均按零需水计算）中，用水节点 "
            + "、".join(baseline_below)
            + " 的水头已低于最低水头限值 "
            f"{net.min_head:g}，现有管网在复供前即欠压，任何复供组合都不可选，"
            "无法复供。")
        return {
            "status": "baseline_below_limit",
            "optimal_confirmed": False,
            "min_head": net.min_head,
            "resupply_nodes": candidates,
            "table_demands": {nid: table_demands[nid] for nid in candidates},
            "table_demand_total": float(sum(table_demands.values())),
            "evaluated_combinations": 1,
            "total_combinations": 1 << k,
            "baseline": baseline,
            "baseline_below_min_head": baseline_below,
            "combinations": [base_record],
            "counts": {"feasible": 0, "below_limit": 1, "not_converged": 0},
            "selected": None,
            "selected_provisional": False,
            "restored_demand_total": None,
            "unrestored_nodes": list(candidates),
            "failed_combinations": [],
            "conclusion": conclusion,
        }
    base_record["status"] = "feasible"

    # ---- 第 2 步：枚举全部非空组合并逐一稳态求解 ---------------------------
    def combo_ids(mask: int) -> list[str]:
        return [candidates[b] for b in range(k) if mask & (1 << b)]

    feasible_combos: list[dict[str, Any]] = [base_record]
    below_combos: list[dict[str, Any]] = []
    failed_combos: list[dict[str, Any]] = []

    for mask in range(1, 1 << k):
        ids = combo_ids(mask)
        restored_total = float(sum(table_demands[nid] for nid in ids))
        combo_net = _network_for_combo(net, cand_set, set(ids))
        try:
            result = solve_network(combo_net)
        except SolverNotConverged as exc:
            failed_combos.append({
                "status": "not_converged",
                "nodes": ids,
                "node_count": len(ids),
                "restored_demand": restored_total,
                "error": str(exc),
            })
            continue
        record = _combo_solution_record(mask, candidates, table_demands, result)
        if record["status"] == "feasible":
            feasible_combos.append(record)
        else:
            below_combos.append(record)

    # ---- 第 3 步：组合排序 -------------------------------------------------
    # 恢复总量降序（1e-9 容差归并同分）→ 节点数降序 → 按录入顺序的字典序
    def cmp_combo(a: dict[str, Any], b: dict[str, Any]) -> int:
        delta = float(b["restored_demand"]) - float(a["restored_demand"])
        if delta > RESUPPLY_TOTAL_TIE_TOL:
            return 1
        if delta < -RESUPPLY_TOTAL_TIE_TOL:
            return -1
        if a["node_count"] != b["node_count"]:
            return -1 if a["node_count"] > b["node_count"] else 1
        ia = tuple(candidates.index(nid) for nid in a["nodes"])
        ib = tuple(candidates.index(nid) for nid in b["nodes"])
        return -1 if ia < ib else (1 if ia > ib else 0)

    feasible_combos.sort(key=cmp_to_key(cmp_combo))
    below_combos.sort(key=cmp_to_key(cmp_combo))
    failed_combos.sort(key=cmp_to_key(cmp_combo))

    all_combos = feasible_combos + below_combos + failed_combos
    for rank, rec in enumerate(all_combos, start=1):
        rec["rank"] = rank

    best = feasible_combos[0] if feasible_combos else None
    counts = {
        "feasible": len(feasible_combos),
        "below_limit": len(below_combos),
        "not_converged": len(failed_combos),
    }

    if failed_combos:
        status = "not_confirmed"
        optimal_confirmed = False
        selected_provisional = best is not None
        failed_desc = "、".join("{" + "、".join(rec["nodes"]) + "}"
                               for rec in failed_combos)
        if best is None:
            conclusion = (
                f"共有 {len(failed_combos)} 个复供组合（{failed_desc}）的稳态求解"
                "未能收敛，且没有任何组合被确认可行；无法确认最优复供组合"
                "（不能把求解失败当作欠压）。")
        else:
            nodes_desc = "、".join(best["nodes"])
            conclusion = (
                f"共有 {len(failed_combos)} 个复供组合（{failed_desc}）的稳态求解"
                "未能收敛，无法在全部组合上确认最优（不能把求解失败当作欠压）；"
                f"当前已确认可行组合中的临时最优为 {{{nodes_desc}}}，"
                f"恢复需水量总和 {best['restored_demand']:g}，"
                "但该结果可能被未能求解的组合超越。")
    else:
        status = "optimal"
        optimal_confirmed = True
        selected_provisional = False
        assert best is not None  # 基线已可行，空集至少是一个可行组合
        if best["node_count"] == 0:
            conclusion = (
                f"全部 {len(all_combos)} 个组合均已稳态求解；除空集外没有任何复供"
                "组合能使所有用水节点水头不低于限值，最优组合为空集（不恢复任何"
                "停供节点），恢复需水量总和为 0。")
        else:
            conclusion = (
                f"全部 {len(all_combos)} 个组合均已稳态收敛；在所有用水节点水头"
                f"均不低于最低水头限值 {net.min_head:g} 的可行组合中，恢复需水量"
                f"总和最大的组合为 {{{'、'.join(best['nodes'])}}}，恢复总量 "
                f"{best['restored_demand']:g}。")

    selected_ids = best["nodes"] if best is not None else []
    return {
        "status": status,
        "optimal_confirmed": optimal_confirmed,
        "min_head": net.min_head,
        "resupply_nodes": candidates,
        "table_demands": {nid: table_demands[nid] for nid in candidates},
        "table_demand_total": float(sum(table_demands.values())),
        "evaluated_combinations": len(all_combos),
        "total_combinations": 1 << k,
        "baseline": baseline,
        "baseline_below_min_head": baseline_below,
        "combinations": all_combos,
        "counts": counts,
        "selected": best,
        "selected_provisional": selected_provisional,
        "restored_demand_total": (None if best is None
                                 else float(best["restored_demand"])),
        "unrestored_nodes": [nid for nid in candidates if nid not in selected_ids],
        "failed_combinations": failed_combos,
        "conclusion": conclusion,
    }
