"""
停管隔离作业 —— 可逐步执行的管段开闭操作与水源连通区核算。

每个作业记录创建时校验通过的管网、“允许暂时失供”的现有用水节点集合（可为空）、
当前关闭管段集合、版本号以及最近一次核算通过的连通区稳态结果；作业只保存在
**单进程内存**中，服务重启即全部清空。

每次操作携带期望版本号并切换**一根**现有管段的开闭，随后按**全部**关闭管段核算：

  1. 仅走开启管段，从水源做无向 BFS 得到水源连通区；
  2. 连通区外的用水节点（失供节点）必须全部落在允许集合内；
  3. 连通区内的用水节点按**原需水量**做稳态求解，且任何用水节点水头都不得低于限值；
  4. 失供节点在作业数据中始终保留原需水量，重新连通后自动恢复参与求解；
  5. 连通区仅剩水源时供水量为零（平凡解，无需迭代）。

只有全部通过才原子提交：关闭集合、版本号 +1、稳态结果**一起**更新；
非法操作、不收敛或规则不满足时一律**保持原状**并抛出带中文原因的异常。
同一版本的并发操作在锁内检查版本号，**最多成功一次**；读取返回的是构造后
不再修改的快照，不会混合不同版本的数据。
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from . import solver
from .solver import Network, SolverNotConverged, ValidationError


class JobNotFoundError(RuntimeError):
    """作业编号不存在（作业仅存于单进程内存，服务重启后即清空）。"""


class VersionConflictError(RuntimeError):
    """携带的版本号与作业当前版本不一致：同一版本的并发操作最多成功一次。"""

    def __init__(self, message: str, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class IsolationRuleError(RuntimeError):
    """切换后的状态违反停管隔离规则（失供越界或连通区欠压）。"""


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def validate_isolation_payload(payload: Any) -> tuple[Network, list[str]]:
    """在管网校验的基础上追加校验允许失供集合，返回 (Network, 允许节点按录入顺序)。"""
    net = solver.validate_payload(payload)
    demand_ids = {n["id"] for n in net.nodes if n["type"] == "demand"}

    raw = payload.get("allowed_unserved_nodes")
    if raw is None:  # 省略或显式 null 都按空集合处理
        raw = []
    if not isinstance(raw, list):
        raise ValidationError(
            "allowed_unserved_nodes 必须是允许暂时失供的现有用水节点编号数组"
            "（可为空数组或省略）。")
    seen: set[str] = set()
    for k, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"允许失供集合的第 {k} 项必须是有效的节点编号（非空字符串）。")
        nid = item.strip()
        if nid not in demand_ids:
            raise ValidationError(
                f"允许失供节点“{nid}”不是已定义的用水节点"
                "（允许集合必须全部为现有用水节点，不能是水源）。")
        if nid in seen:
            raise ValidationError(
                f"允许失供节点“{nid}”重复，允许集合中的节点必须互异。")
        seen.add(nid)
    # 一律按节点录入顺序保存，与数组书写顺序无关
    return net, [n["id"] for n in net.nodes if n["id"] in seen]


# --------------------------------------------------------------------------- #
# 水源连通区核算
# --------------------------------------------------------------------------- #
def _source_component(net: Network, closed: frozenset[str]) -> set[str]:
    """仅走开启管段，从水源做无向 BFS，返回水源连通区的节点编号集合。"""
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
    return seen


def _source_only_solution(net: Network, source: dict[str, Any]) -> dict[str, Any]:
    """连通区仅剩水源时的平凡解：供水量为零，无任何管段流量。"""
    h = float(source["head"])
    below = bool(h < net.min_head)
    return {
        "converged": True,
        "tolerance": solver.CHECK_TOL,
        "method": "trivial-source-only",
        "iterations": 0,
        "solver_residual_inf": 0.0,
        "node_results": [{
            "id": source["id"],
            "type": "source",
            "head": h,
            "net_inflow": 0.0,
            "below_min_head": below,
            "fixed_head": h,
            "outflow": 0.0,
        }],
        "pipe_results": [],
        "heads": {source["id"]: h},
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


def _evaluate(net: Network, allowed_set: frozenset[str],
              closed: frozenset[str]
              ) -> tuple[list[str], list[str], dict[str, Any]]:
    """按全部关闭管段核算，返回 (连通区节点, 失供节点, 连通区稳态结果)。

    失供越界或连通区欠压时抛 IsolationRuleError；连通区求解不收敛时抛
    SolverNotConverged。两种情况下调用方都必须保持原状。
    """
    reachable = _source_component(net, closed)
    connected = [n["id"] for n in net.nodes if n["id"] in reachable]
    unserved = [n["id"] for n in net.nodes
                if n["type"] == "demand" and n["id"] not in reachable]

    illegal = [nid for nid in unserved if nid not in allowed_set]
    if illegal:
        raise IsolationRuleError(
            "切换后用水节点 " + "、".join(illegal) +
            " 将脱离水源连通区而失供，但不在允许暂时失供的集合内；"
            "操作已被拒绝，关闭集合、版本与稳态结果均保持原状。")

    # 开启管段的两端必然同在连通区内或同在其外，按起点筛选即可
    comp_nodes = [n for n in net.nodes if n["id"] in reachable]
    comp_pipes = [p for p in net.pipes
                  if p["id"] not in closed and p["start"] in reachable]
    served = [n for n in comp_nodes if n["type"] == "demand"]

    if not served:
        # 连通区仅剩水源：供水量为零的平凡解
        source = next(n for n in comp_nodes if n["type"] == "source")
        return connected, unserved, _source_only_solution(net, source)

    sub = Network(nodes=comp_nodes, pipes=comp_pipes, min_head=net.min_head)
    try:
        result = solver.solve_network(sub)
    except SolverNotConverged as exc:
        raise SolverNotConverged(
            f"切换后水源连通区的稳态求解未能收敛（{exc}）；"
            "操作已被拒绝，关闭集合、版本与稳态结果均保持原状。") from exc
    below = [n["id"] for n in served
             if result["heads"][n["id"]] < net.min_head]
    if below:
        raise IsolationRuleError(
            "切换后水源连通区内的用水节点 " + "、".join(below) +
            f" 的水头将低于最低水头限值 {net.min_head:g}；"
            "操作已被拒绝，关闭集合、版本与稳态结果均保持原状。")
    return connected, unserved, result


# --------------------------------------------------------------------------- #
# 作业与快照
# --------------------------------------------------------------------------- #
@dataclass
class IsolationJob:
    """一个停管隔离作业的全部可变状态（仅存在于单进程内存）。"""
    job_id: str
    network: Network
    allowed: list[str]            # 允许暂时失供的用水节点，按节点录入顺序
    allowed_set: frozenset[str]
    pipe_ids: frozenset[str]
    version: int
    closed: frozenset[str]
    snapshot: dict[str, Any]      # 当前版本对外快照；构造后不再被修改


def _build_snapshot(job: IsolationJob, version: int, closed: frozenset[str],
                    connected: list[str], unserved: list[str],
                    result: dict[str, Any]) -> dict[str, Any]:
    """把一次核算通过的状态组装成对外快照（关闭管段、失供节点、连通区结果）。"""
    net = job.network
    demands = {n["id"]: float(n["demand"])
               for n in net.nodes if n["type"] == "demand"}
    return {
        "job_id": job.job_id,
        "version": version,
        "min_head": net.min_head,
        "source_node": next(n["id"] for n in net.nodes if n["type"] == "source"),
        "allowed_unserved_nodes": list(job.allowed),
        "closed_pipes": [p["id"] for p in net.pipes if p["id"] in closed],
        "open_pipes": [p["id"] for p in net.pipes if p["id"] not in closed],
        "connected_nodes": list(connected),
        "served_nodes": [nid for nid in connected if nid in demands],
        "unserved_nodes": list(unserved),
        "unserved_demand": float(sum(demands[nid] for nid in unserved)),
        "solution": result,
    }


# --------------------------------------------------------------------------- #
# 单进程内存作业仓库
# --------------------------------------------------------------------------- #
class IsolationStore:
    """单进程内存作业仓库：重启即清空；状态变更在锁内原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, IsolationJob] = {}

    # ---- 创建 -------------------------------------------------------------
    def create(self, payload: Any) -> dict[str, Any]:
        """校验并创建作业：管段初始全开，返回版本 0 的初始快照。

        初始状态（全部管段开启）不收敛或任一用水节点欠压时不创建作业。
        """
        net, allowed = validate_isolation_payload(payload)
        try:
            result = solver.solve_network(net)
        except SolverNotConverged as exc:
            raise SolverNotConverged(
                "初始状态（全部管段开启）的稳态求解未能收敛，未创建作业："
                f"{exc}") from exc
        below = [n["id"] for n in net.nodes
                 if n["type"] == "demand"
                 and result["heads"][n["id"]] < net.min_head]
        if below:
            raise IsolationRuleError(
                "初始状态（全部管段开启）中用水节点 " + "、".join(below) +
                f" 的水头已低于最低水头限值 {net.min_head:g}，未创建作业。")

        job = IsolationJob(
            job_id=uuid.uuid4().hex,
            network=net,
            allowed=allowed,
            allowed_set=frozenset(allowed),
            pipe_ids=frozenset(p["id"] for p in net.pipes),
            version=0,
            closed=frozenset(),
            snapshot={},
        )
        connected = [n["id"] for n in net.nodes]
        job.snapshot = _build_snapshot(job, 0, frozenset(), connected, [], result)
        with self._lock:
            self._jobs[job.job_id] = job
        return job.snapshot

    # ---- 读取 -------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any]:
        """返回当前版本的完整快照；快照构造后不再修改，不会混合不同版本的数据。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(
                    f"作业“{job_id}”不存在"
                    "（作业仅存于单进程内存，服务重启后即清空）。")
            return job.snapshot

    # ---- 切换一根现有管段的开闭 ---------------------------------------------
    def toggle(self, job_id: str, payload: Any) -> dict[str, Any]:
        """携带期望版本号切换一根现有管段的开闭；全部规则通过才原子提交新版本。"""
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是包含 pipe_id 与 version 的对象。")
        pipe_id = payload.get("pipe_id")
        if not isinstance(pipe_id, str) or not pipe_id.strip():
            raise ValidationError(
                "pipe_id 必须是要切换开闭的现有管段编号（非空字符串）。")
        pipe_id = pipe_id.strip()
        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError(
                "version 必须是整数（读取作业时获得的当前版本号）。")

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(
                    f"作业“{job_id}”不存在"
                    "（作业仅存于单进程内存，服务重启后即清空）。")
            if pipe_id not in job.pipe_ids:
                raise ValidationError(
                    f"管段“{pipe_id}”不在作业“{job_id}”的管网中，"
                    "只能切换现有管段的开闭。")
            if version != job.version:
                raise VersionConflictError(
                    f"操作基于版本 {version}，但作业当前版本已是 {job.version}；"
                    "同一版本的并发操作最多成功一次，请重新读取最新状态后"
                    "携带新版本号重试。",
                    current_version=job.version)

            new_closed = job.closed ^ {pipe_id}  # 开 ↔ 闭 切换
            connected, unserved, result = _evaluate(
                job.network, job.allowed_set, new_closed)

            # 全部规则通过才一起更新：关闭集合、版本 +1、稳态结果
            job.closed = new_closed
            job.version = version + 1
            job.snapshot = _build_snapshot(
                job, job.version, new_closed, connected, unserved, result)
            return job.snapshot
