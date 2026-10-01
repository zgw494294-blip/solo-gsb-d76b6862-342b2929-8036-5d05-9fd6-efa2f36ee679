"""
停管隔离作业 —— 可逐步执行的停管/复管操作（仅存于单进程内存，重启即清空）。

每个作业记录：创建时校验通过的管网（沿用 solver 的校验与稳态求解）、允许暂时
失供的用水节点集合（可为空）、当前关闭管段集合、版本号与最近一次成功计算的
状态快照。管段初始全部开启。

每次“切换一根现有管段的开闭”都按全部关闭管段重新计算水源连通区：

  * 失供节点（与水源断开的用水节点）必须全部在允许暂时失供的集合内；
  * 连通区内用水节点按原需水量求解，且任何用水节点水头不得低于最低限值；
  * 失供节点保留原需水量，重新连通后自动恢复参与求解；
  * 连通区仅剩水源时供水量为零（不再调用求解器）。

只有全部规则满足且稳态求解收敛时才一次性提交：关闭集合、版本号 +1、状态快照；
非法操作、不收敛或规则不满足时全部保持原状并说明原因。版本号即乐观锁：
同一版本的并发切换最多成功一次；读取始终返回某一版本的完整快照，绝不混合
不同版本的数据。
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .solver import (
    CHECK_TOL,
    Network,
    SolverNotConverged,
    ValidationError,
    solve_network,
    validate_payload,
)


class JobNotFoundError(KeyError):
    """作业编号不存在（作业仅存于单进程内存，服务重启后即清空）。"""


class VersionConflictError(RuntimeError):
    """请求携带的版本与作业当前版本不一致：同一版本最多允许一次成功切换。"""

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            f"版本冲突：请求携带的版本为 {expected}，作业当前版本已为 {current}；"
            "同一版本的切换最多成功一次，请重新读取最新状态后再试。")
        self.expected = expected
        self.current = current


class IsolationRuleError(RuntimeError):
    """切换后不满足停管隔离规则（或连通区求解不收敛）：作业状态全部保持原状。"""


@dataclass
class _IsolationJob:
    """单个停管隔离作业的全部可变状态（仅在持有自身锁时修改）。"""
    job_id: str
    network: Network
    allowed_unserved: frozenset[str]
    closed_pipes: frozenset[str]
    version: int
    snapshot: dict[str, Any]
    lock: threading.Lock = field(default_factory=threading.Lock)


# 作业注册表：仅存于单进程内存，服务重启即清空
_JOBS: dict[str, _IsolationJob] = {}
_REGISTRY_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _validate_allow_unserved(raw: Any, net: Network) -> frozenset[str]:
    """校验允许暂时失供的用水节点集合（可省略或为空数组），返回 frozenset。"""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValidationError(
            "allow_unserved 必须是允许暂时失供的用水节点编号数组（可为空数组）。")
    demand_ids = {n["id"] for n in net.nodes if n["type"] == "demand"}
    seen: set[str] = set()
    for k, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"允许失供集合第 {k} 项缺少有效的节点编号（非空字符串）。")
        nid = item.strip()
        if nid not in demand_ids:
            raise ValidationError(
                f"允许失供节点“{nid}”不是已定义的用水节点"
                "（集合内必须全部为用水节点，不能是水源或未定义节点）。")
        if nid in seen:
            raise ValidationError(f"允许失供节点“{nid}”重复，集合内节点必须互异。")
        seen.add(nid)
    return frozenset(seen)


# --------------------------------------------------------------------------- #
# 状态计算
# --------------------------------------------------------------------------- #
def _connected_component(net: Network, closed: frozenset[str]):
    """按全部关闭管段做无向 BFS，返回（连通区节点编号、连通区内开启管段），均按录入顺序。"""
    source_id = next(n["id"] for n in net.nodes if n["type"] == "source")
    adj: dict[str, list[str]] = {n["id"]: [] for n in net.nodes}
    for pipe in net.pipes:
        if pipe["id"] in closed:
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
    nodes = [n["id"] for n in net.nodes if n["id"] in seen]
    # 开启管段的一个端点可达，另一个端点必然也可达
    pipes = [p for p in net.pipes if p["id"] not in closed and p["start"] in seen]
    return nodes, pipes


def _source_only_solution(net: Network, source: dict[str, Any]) -> dict[str, Any]:
    """连通区仅剩水源时的平凡解：不调用求解器，供水量为零，键与 solve_network 一致。"""
    head = float(source["head"])
    below = head < net.min_head
    return {
        "converged": True,
        "tolerance": CHECK_TOL,
        "method": "source-only",
        "iterations": 0,
        "solver_residual_inf": 0.0,
        "node_results": [{
            "id": source["id"],
            "type": "source",
            "head": head,
            "net_inflow": 0.0,
            "below_min_head": below,
            "fixed_head": head,
            "outflow": 0.0,
        }],
        "pipe_results": [],
        "heads": {source["id"]: head},
        "flows": {},
        "source_node": source["id"],
        "source_supply": 0.0,
        "total_demand": 0.0,
        "balance_residual": 0.0,
        "max_node_residual": 0.0,
        "max_pipe_residual": 0.0,
        "min_head": net.min_head,
        "below_min_head": [source["id"]] if below else [],
    }


def _build_snapshot(job_id: str, version: int, net: Network,
                    allowed: frozenset[str], closed: frozenset[str],
                    *, create: bool) -> dict[str, Any]:
    """计算某一关闭集合对应的状态快照；规则不满足或不收敛时抛 IsolationRuleError。

    create=True 用于创建时的初始状态（全部管段开启），失败文案为“不创建作业”；
    create=False 用于切换管段，失败文案为“本次操作未生效，作业保持原状”。
    """
    source_id = next(n["id"] for n in net.nodes if n["type"] == "source")
    demand_map = {n["id"]: n["demand"] for n in net.nodes if n["type"] == "demand"}

    component_nodes, component_pipes = _connected_component(net, closed)
    component_set = set(component_nodes)
    # 失供节点保留原需水量（network 永不变更），重连后自动恢复参与求解
    unserved = [nid for nid in demand_map if nid not in component_set]

    # 规则 1：失供节点必须全部在允许暂时失供的集合内
    outside = [nid for nid in unserved if nid not in allowed]
    if outside:
        names = "、".join(f"“{nid}”" for nid in outside)
        if create:
            raise IsolationRuleError(
                f"初始状态下用水节点 {names} 即失供，且不在允许暂时失供的集合内，"
                "不创建停管隔离作业。")
        raise IsolationRuleError(
            f"切换后用水节点 {names} 将失供，但其不在允许暂时失供的节点集合内；"
            "本次操作未生效，关闭集合、版本与稳态结果全部保持原状。")

    # 规则 2：连通区用水节点按原需水量求解且不得欠压；仅剩水源时供水量为零
    if len(component_nodes) == 1:
        solution = _source_only_solution(
            net, next(n for n in net.nodes if n["type"] == "source"))
    else:
        sub_net = Network(
            nodes=[n for n in net.nodes if n["id"] in component_set],
            pipes=component_pipes,
            min_head=net.min_head,
        )
        try:
            solution = solve_network(sub_net)
        except SolverNotConverged as exc:
            if create:
                raise IsolationRuleError(
                    "初始状态（全部管段开启）的稳态求解未能收敛，不创建停管隔离作业。"
                    f"（{exc}）") from exc
            raise IsolationRuleError(
                "切换后连通区的稳态求解未能收敛；本次操作未生效，关闭集合、版本与"
                f"稳态结果全部保持原状。（{exc}）") from exc
        below = [nid for nid in component_nodes
                 if nid in demand_map and solution["heads"][nid] < net.min_head]
        if below:
            names = "、".join(f"“{nid}”" for nid in below)
            if create:
                raise IsolationRuleError(
                    f"初始状态（全部管段开启）下，用水节点 {names} 的水头低于最低"
                    f"水头限值 {net.min_head:g}，不创建停管隔离作业。")
            raise IsolationRuleError(
                f"切换后连通区内用水节点 {names} 的水头将低于最低水头限值 "
                f"{net.min_head:g}；本次操作未生效，关闭集合、版本与稳态结果全部保持原状。")

    return {
        "job_id": job_id,
        "version": version,
        "min_head": net.min_head,
        "source_node": source_id,
        "allowed_unserved": [nid for nid in demand_map if nid in allowed],
        "closed_pipes": [p["id"] for p in net.pipes if p["id"] in closed],
        "open_pipes": [p["id"] for p in net.pipes if p["id"] not in closed],
        "pipe_states": [
            {"id": p["id"], "start": p["start"], "end": p["end"],
             "closed": p["id"] in closed}
            for p in net.pipes
        ],
        "unserved_nodes": unserved,
        "unserved_demand": float(sum(demand_map[nid] for nid in unserved)),
        "component_nodes": component_nodes,
        "component_pipes": [p["id"] for p in component_pipes],
        "source_only": len(component_nodes) == 1,
        "source_supply": solution["source_supply"],
        "total_demand": solution["total_demand"],
        "solution": solution,
    }


# --------------------------------------------------------------------------- #
# 作业生命周期：创建 / 读取 / 切换
# --------------------------------------------------------------------------- #
def create_job(payload: Any) -> dict[str, Any]:
    """创建停管隔离作业，返回版本 0 的初始快照。

    沿用同一套管网校验与稳态求解；校验失败、初始有用水节点欠压或初始求解
    不收敛时抛异常，不创建作业。
    """
    net = validate_payload(payload)
    allowed = _validate_allow_unserved(payload.get("allow_unserved"), net)
    job_id = uuid.uuid4().hex
    snapshot = _build_snapshot(job_id, 0, net, allowed, frozenset(), create=True)
    job = _IsolationJob(
        job_id=job_id,
        network=net,
        allowed_unserved=allowed,
        closed_pipes=frozenset(),
        version=0,
        snapshot=snapshot,
    )
    with _REGISTRY_LOCK:
        _JOBS[job_id] = job
    return snapshot


def get_snapshot(job_id: str) -> dict[str, Any]:
    """读取作业当前版本的完整快照（快照为不可变整体，绝不混合不同版本的数据）。"""
    job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    with job.lock:
        return job.snapshot


def toggle_pipe(job_id: str, payload: Any) -> dict[str, Any]:
    """携带当前版本切换一根现有管段的开闭，返回新版本快照。

    版本不一致抛 VersionConflictError（同一版本的并发切换最多成功一次）；
    管段不存在、规则不满足或求解不收敛时抛异常，关闭集合、版本号与稳态
    结果全部保持原状。
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是包含 version 与 pipe_id 的对象。")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("version 必须是非负整数（即作业当前版本号）。")
    pipe_id = payload.get("pipe_id")
    if not isinstance(pipe_id, str) or not pipe_id.strip():
        raise ValidationError("pipe_id 必须是要切换开闭的现有管段编号（非空字符串）。")
    pipe_id = pipe_id.strip()

    with job.lock:
        if version != job.version:
            raise VersionConflictError(version, job.version)
        if pipe_id not in {p["id"] for p in job.network.pipes}:
            raise ValidationError(
                f"管段“{pipe_id}”不是该作业管网中已定义的管段。")
        new_closed = job.closed_pipes ^ {pipe_id}  # 开→关 或 关→开
        snapshot = _build_snapshot(job.job_id, job.version + 1, job.network,
                                   job.allowed_unserved, new_closed, create=False)
        # 全部规则满足且求解收敛：一次性提交关闭集合、版本号与稳态结果
        job.closed_pipes = new_closed
        job.version += 1
        job.snapshot = snapshot
        return snapshot
