"""停管隔离作业的端到端测试（pytest）。

覆盖：创建（含初始欠压/不收敛不创建）、允许失供集合校验、切换开闭的
失供/欠压/重连/仅剩水源规则、版本冲突与并发最多成功一次、读取一致性、
非法输入与各 HTTP 状态码分支。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.isolation import (
    IsolationRuleError,
    IsolationStore,
    JobNotFoundError,
    VersionConflictError,
    validate_isolation_payload,
)
from app.main import app
from app.solver import (
    Network,
    SolverNotConverged,
    ValidationError,
    solve_network,
    validate_payload,
)

client = TestClient(app)


def _tree_payload(allowed=None):
    """树状网 S–A–B–C（各需水 1）：P1/P2/P3 都是桥，全开时各点水头 21/17/16。"""
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "C", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "C", "r": 1.0},
        ],
        "min_head": 0.0,
    }
    if allowed is not None:
        payload["allowed_unserved_nodes"] = allowed
    return payload


def _loop_payload():
    """4 节点环网（S-A-B-C-S + 对角线 S-B，r=2.5），各需水 1，去掉任一根仍连通。"""
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "C", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "C", "r": 1.0},
            {"id": "P4", "start": "C", "end": "S", "r": 1.0},
            {"id": "P5", "start": "S", "end": "B", "r": 2.5},
        ],
        "min_head": 0.0,
        "allowed_unserved_nodes": [],
    }


def _create(payload):
    """经 API 创建作业并断言成功，返回快照。"""
    resp = client.post("/api/isolation-jobs", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    return data["job"]


# ---------------------------- 创建 ---------------------------- #
def test_create_returns_version_zero_and_initial_state():
    job = _create(_loop_payload())
    assert job["job_id"]
    assert job["version"] == 0
    assert job["closed_pipes"] == []
    assert job["open_pipes"] == ["P1", "P2", "P3", "P4", "P5"]
    assert job["unserved_nodes"] == []
    assert job["unserved_demand"] == pytest.approx(0.0)
    assert job["connected_nodes"] == ["S", "A", "B", "C"]
    assert job["served_nodes"] == ["A", "B", "C"]
    assert job["allowed_unserved_nodes"] == []
    sol = job["solution"]
    assert sol["converged"] is True
    assert sol["source_supply"] == pytest.approx(3.0, abs=1e-9)
    # 读取同一作业：快照与创建时完全一致（同一版本数据）
    got = client.get(f"/api/isolation-jobs/{job['job_id']}")
    assert got.status_code == 200
    assert got.json()["job"] == job


def test_create_allowed_set_defaults_to_empty_and_entry_order():
    # 省略 allowed_unserved_nodes -> 空集合
    job = _create(_tree_payload())
    assert job["allowed_unserved_nodes"] == []
    # 书写顺序乱序 -> 一律按节点录入顺序保存
    job2 = _create(_tree_payload(allowed=["C", "A"]))
    assert job2["allowed_unserved_nodes"] == ["A", "C"]


def test_create_initial_below_min_head_not_created():
    store = IsolationStore()
    payload = _tree_payload()
    payload["min_head"] = 35.0  # 初始各点水头最高 30，全部欠压
    with pytest.raises(IsolationRuleError, match="未创建作业"):
        store.create(payload)
    assert store._jobs == {}  # 不创建作业

    resp = client.post("/api/isolation-jobs", json=payload)
    assert resp.status_code == 422
    assert "未创建作业" in resp.json()["error"]


def test_create_initial_not_converged_not_created(monkeypatch):
    def boom(net):
        raise SolverNotConverged("模拟初始不收敛")

    monkeypatch.setattr("app.solver.solve_network", boom)
    store = IsolationStore()
    with pytest.raises(SolverNotConverged, match="未创建作业"):
        store.create(_tree_payload())
    assert store._jobs == {}

    resp = client.post("/api/isolation-jobs", json=_tree_payload())
    assert resp.status_code == 422
    assert "未能收敛" in resp.json()["error"]


def test_allowed_set_validation():
    def expect_error(mutate, fragment):
        p = _tree_payload(["A"])
        mutate(p)
        with pytest.raises(ValidationError) as exc:
            validate_isolation_payload(p)
        assert fragment in str(exc.value)

    expect_error(lambda p: p.update(allowed_unserved_nodes="A"), "数组")
    expect_error(lambda p: p.update(allowed_unserved_nodes=[123]), "非空字符串")
    expect_error(lambda p: p.update(allowed_unserved_nodes=["ZZ"]), "用水节点")
    expect_error(lambda p: p.update(allowed_unserved_nodes=["S"]), "用水节点")
    expect_error(lambda p: p.update(allowed_unserved_nodes=["A", "A"]), "重复")

    # 原有管网校验仍然生效
    bad = _tree_payload(["A"])
    bad["pipes"][0]["r"] = 0
    with pytest.raises(ValidationError, match="阻力"):
        validate_isolation_payload(bad)

    resp = client.post("/api/isolation-jobs",
                       json=_tree_payload(allowed=["S"]))
    assert resp.status_code == 422
    assert "用水节点" in resp.json()["error"]


# ---------------------------- 切换：失供与恢复 ---------------------------- #
def test_toggle_close_disconnects_within_allowed():
    job = _create(_tree_payload(allowed=["B", "C"]))
    resp = client.post(f"/api/isolation-jobs/{job['job_id']}/toggle",
                       json={"pipe_id": "P2", "version": 0})
    assert resp.status_code == 200
    new = resp.json()["job"]
    assert new["version"] == 1
    assert new["closed_pipes"] == ["P2"]
    assert new["open_pipes"] == ["P1", "P3"]
    # B、C 失供（保留原需水量），连通区仅剩 S、A
    assert new["unserved_nodes"] == ["B", "C"]
    assert new["unserved_demand"] == pytest.approx(2.0)
    assert new["connected_nodes"] == ["S", "A"]
    assert new["served_nodes"] == ["A"]
    sol = new["solution"]
    assert sol["source_supply"] == pytest.approx(1.0, abs=1e-9)
    assert sol["total_demand"] == pytest.approx(1.0, abs=1e-9)
    assert set(sol["heads"]) == {"S", "A"}
    assert set(sol["flows"]) == {"P1"}
    assert sol["heads"]["A"] == pytest.approx(29.0, abs=1e-9)


def test_toggle_unserved_outside_allowed_rejected_and_unchanged():
    job = _create(_tree_payload(allowed=["C"]))  # B 不在允许集合内
    resp = client.post(f"/api/isolation-jobs/{job['job_id']}/toggle",
                       json={"pipe_id": "P2", "version": 0})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "允许暂时失供" in err and "B" in err and "保持原状" in err
    # 全部保持原状
    cur = client.get(f"/api/isolation-jobs/{job['job_id']}").json()["job"]
    assert cur["version"] == 0
    assert cur["closed_pipes"] == []
    assert cur["unserved_nodes"] == []
    assert cur["solution"]["source_supply"] == pytest.approx(3.0, abs=1e-9)


def test_toggle_below_min_head_rejected_and_unchanged():
    payload = _loop_payload()
    net = validate_payload(payload)
    demand_ids = ["A", "B", "C"]
    baseline = solve_network(net)
    base_min = min(baseline["heads"][nid] for nid in demand_ids)
    # 找出移除后最低用水节点水头最差的管段
    worst_head, worst_pipe = None, None
    for i, pipe in enumerate(net.pipes):
        sub = Network(nodes=net.nodes,
                      pipes=net.pipes[:i] + net.pipes[i + 1:],
                      min_head=net.min_head)
        res = solve_network(sub)
        h = min(res["heads"][nid] for nid in demand_ids)
        if worst_head is None or h < worst_head:
            worst_head, worst_pipe = h, pipe["id"]
    # 限值取在基线与最差情景之间：创建合格，切换后欠压
    payload["min_head"] = (base_min + worst_head) / 2
    job = _create(payload)

    resp = client.post(f"/api/isolation-jobs/{job['job_id']}/toggle",
                       json={"pipe_id": worst_pipe, "version": 0})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "低于最低水头限值" in err and "保持原状" in err
    cur = client.get(f"/api/isolation-jobs/{job['job_id']}").json()["job"]
    assert cur["version"] == 0
    assert cur["closed_pipes"] == []


def test_toggle_reopen_restores_original_demand():
    """失供节点保留原需水量：重连后按原需水量恢复参与求解。"""
    job = _create(_tree_payload(allowed=["B", "C"]))
    jid = job["job_id"]
    r1 = client.post(f"/api/isolation-jobs/{jid}/toggle",
                     json={"pipe_id": "P2", "version": 0})
    assert r1.status_code == 200
    assert r1.json()["job"]["unserved_nodes"] == ["B", "C"]
    # 再次切换 P2 = 重开，B、C 以原需水量 1 恢复参与求解
    r2 = client.post(f"/api/isolation-jobs/{jid}/toggle",
                     json={"pipe_id": "P2", "version": 1})
    assert r2.status_code == 200
    job2 = r2.json()["job"]
    assert job2["version"] == 2
    assert job2["closed_pipes"] == []
    assert job2["unserved_nodes"] == []
    sol = job2["solution"]
    assert sol["source_supply"] == pytest.approx(3.0, abs=1e-9)
    assert sol["heads"]["C"] == pytest.approx(16.0, abs=1e-9)


def test_toggle_to_source_only_zero_supply():
    """仅剩水源时供水量为零。"""
    job = _create(_tree_payload(allowed=["A", "B", "C"]))
    resp = client.post(f"/api/isolation-jobs/{job['job_id']}/toggle",
                       json={"pipe_id": "P1", "version": 0})
    assert resp.status_code == 200
    new = resp.json()["job"]
    assert new["version"] == 1
    assert new["connected_nodes"] == ["S"]
    assert new["served_nodes"] == []
    assert new["unserved_nodes"] == ["A", "B", "C"]
    assert new["unserved_demand"] == pytest.approx(3.0)
    sol = new["solution"]
    assert sol["source_supply"] == pytest.approx(0.0, abs=1e-12)
    assert sol["total_demand"] == pytest.approx(0.0, abs=1e-12)
    assert sol["heads"] == {"S": 30.0}
    assert sol["flows"] == {}
    assert sol["pipe_results"] == []


# ---------------------------- 版本与并发 ---------------------------- #
def test_toggle_version_conflict_409_and_state_unchanged():
    job = _create(_tree_payload(allowed=["A", "B", "C"]))
    jid = job["job_id"]
    r1 = client.post(f"/api/isolation-jobs/{jid}/toggle",
                     json={"pipe_id": "P1", "version": 0})
    assert r1.status_code == 200
    assert r1.json()["job"]["version"] == 1
    # 同一版本号的第二次操作：冲突，状态不变
    r2 = client.post(f"/api/isolation-jobs/{jid}/toggle",
                     json={"pipe_id": "P2", "version": 0})
    assert r2.status_code == 409
    body = r2.json()
    assert body["ok"] is False
    assert body["current_version"] == 1
    assert "最多成功一次" in body["error"]
    cur = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert cur["version"] == 1
    assert cur["closed_pipes"] == ["P1"]


def test_concurrent_same_version_only_one_succeeds():
    """同一版本的并发切换在仓库层最多成功一次。"""
    store = IsolationStore()
    job = store.create(_tree_payload(allowed=["A", "B", "C"]))
    jid = job["job_id"]

    successes: list[dict] = []
    conflicts: list[VersionConflictError] = []

    def worker(pipe_id: str) -> None:
        try:
            successes.append(
                store.toggle(jid, {"pipe_id": pipe_id, "version": 0}))
        except VersionConflictError as exc:
            conflicts.append(exc)

    threads = [threading.Thread(target=worker, args=(pid,))
               for pid in ("P1", "P2", "P3")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1
    assert len(conflicts) == 2
    final = store.get(jid)
    assert final["version"] == 1
    assert len(final["closed_pipes"]) == 1
    # 成功提交的快照自身一致：关闭集合与连通区结果对应同一版本
    assert final["solution"]["source_supply"] == pytest.approx(
        {  # 关闭 P1 -> 仅剩水源；关闭 P2 -> 供 A；关闭 P3 -> 供 A+B
            "P1": 0.0, "P2": 1.0, "P3": 2.0,
        }[final["closed_pipes"][0]], abs=1e-9)


def test_read_never_mixes_versions():
    """连续两次切换后，读取到的快照完整属于最新版本。"""
    job = _create(_tree_payload(allowed=["A", "B", "C"]))
    jid = job["job_id"]
    client.post(f"/api/isolation-jobs/{jid}/toggle",
                json={"pipe_id": "P1", "version": 0})
    client.post(f"/api/isolation-jobs/{jid}/toggle",
                json={"pipe_id": "P3", "version": 1})
    cur = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert cur["version"] == 2
    assert cur["closed_pipes"] == ["P1", "P3"]
    assert cur["connected_nodes"] == ["S"]
    assert cur["unserved_nodes"] == ["A", "B", "C"]
    assert cur["solution"]["source_supply"] == pytest.approx(0.0, abs=1e-12)


# ---------------------------- 非法操作与异常分支 ---------------------------- #
def test_toggle_unknown_pipe_and_bad_version_rejected():
    job = _create(_tree_payload(allowed=["A"]))
    jid = job["job_id"]
    for body, fragment in (
        ({"pipe_id": "PX", "version": 0}, "不在作业"),
        ({"pipe_id": "", "version": 0}, "pipe_id"),
        ({"version": 0}, "pipe_id"),
        ({"pipe_id": "P1"}, "version"),
        ({"pipe_id": "P1", "version": "0"}, "version"),
        ({"pipe_id": "P1", "version": 1.5}, "version"),
        ({"pipe_id": "P1", "version": True}, "version"),
    ):
        resp = client.post(f"/api/isolation-jobs/{jid}/toggle", json=body)
        assert resp.status_code == 422, body
        assert fragment in resp.json()["error"]
    # 非法操作后状态不变
    cur = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert cur["version"] == 0 and cur["closed_pipes"] == []


def test_toggle_not_converged_keeps_state(monkeypatch):
    job = _create(_tree_payload(allowed=["A", "B", "C"]))
    jid = job["job_id"]

    def boom(net):
        raise SolverNotConverged("模拟切换后不收敛")

    monkeypatch.setattr("app.solver.solve_network", boom)
    resp = client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"pipe_id": "P2", "version": 0})
    assert resp.status_code == 422
    assert "未能收敛" in resp.json()["error"]
    cur = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert cur["version"] == 0
    assert cur["closed_pipes"] == []


def test_unknown_job_404():
    resp = client.get("/api/isolation-jobs/no-such-job")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]
    resp = client.post("/api/isolation-jobs/no-such-job/toggle",
                       json={"pipe_id": "P1", "version": 0})
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]


def test_isolation_empty_body_and_nan_token():
    resp = client.post("/api/isolation-jobs", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    job = _create(_tree_payload())
    resp = client.post(f"/api/isolation-jobs/{job['job_id']}/toggle",
                       content=b"", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    body = ('{"nodes":[{"id":"S","type":"source","head":30},'
            '{"id":"A","type":"demand","demand":1}],'
            '"pipes":[{"id":"P1","start":"S","end":"A","r":1}],'
            '"min_head":0,"allowed_unserved_nodes":[NaN]}')
    resp = client.post("/api/isolation-jobs", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]


def test_store_level_exceptions():
    """仓库层异常类型：未找到 / 版本冲突 / 规则不满足。"""
    store = IsolationStore()
    with pytest.raises(JobNotFoundError):
        store.get("nope")
    job = store.create(_tree_payload(allowed=["B", "C"]))
    jid = job["job_id"]
    with pytest.raises(VersionConflictError) as exc:
        store.toggle(jid, {"pipe_id": "P1", "version": 7})
    assert exc.value.current_version == 0
    # P1 关闭会让 A 也失供，而允许集合只有 B、C
    with pytest.raises(IsolationRuleError, match="允许暂时失供"):
        store.toggle(jid, {"pipe_id": "P1", "version": 0})
    # 失败后原状
    assert store.get(jid)["version"] == 0
    assert store.get(jid)["closed_pipes"] == []
