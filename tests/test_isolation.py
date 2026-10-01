"""停管隔离作业的端到端测试（pytest）。

覆盖：创建（含初始欠压/不收敛不创建）、allow_unserved 校验、切换管段的
失供/欠压/不收敛规则、仅剩水源供水量为零、重连恢复原需水量、版本冲突
（含多线程并发同一版本最多成功一次）、读取快照一致性、404/400/422 分支。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import app.isolation as iso
from app.isolation import (
    VersionConflictError,
    create_job,
    get_snapshot,
    toggle_pipe,
)
from app.main import app
from app.solver import SolverNotConverged

client = TestClient(app)


def _tree_payload():
    """树状网：S(50) ─P1→ A(需2) ─P2→ B(需3)；P1、P2 都是桥。

    基线：q_P1=5、q_P2=3，H_A=25、H_B=16，供水量 5。
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 50.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 3.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
        ],
        "min_head": 0.0,
    }


def _loop_payload():
    """环网：S(30)，A/B 各需 1；P3(S-B) 阻力 100。

    基线各点水头约 26+（合格）；关掉 P1 或 P2 后全部流量压上 P3，
    B 点水头必跌破 0（欠压）；只有关 P3 是合法操作（H_A=26、H_B=25）。
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "S", "end": "B", "r": 100.0},
        ],
        "min_head": 0.0,
    }


def _create(payload) -> dict:
    resp = client.post("/api/isolation-jobs", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    return resp.json()["job"]


def _toggle(job_id: str, version: int, pipe_id: str):
    return client.post(f"/api/isolation-jobs/{job_id}/toggle",
                       json={"version": version, "pipe_id": pipe_id})


def _get(job_id: str) -> dict:
    resp = client.get(f"/api/isolation-jobs/{job_id}")
    assert resp.status_code == 200
    return resp.json()["job"]


# ---------------------------- 创建作业 ---------------------------- #
def test_create_job_initial_state_and_read():
    job = _create(_tree_payload())
    assert job["job_id"]
    assert job["version"] == 0
    assert job["source_node"] == "S"
    assert job["min_head"] == pytest.approx(0.0)
    assert job["allowed_unserved"] == []  # 省略时为空集合
    assert job["closed_pipes"] == [] and job["open_pipes"] == ["P1", "P2"]
    assert job["pipe_states"] == [
        {"id": "P1", "start": "S", "end": "A", "closed": False},
        {"id": "P2", "start": "A", "end": "B", "closed": False},
    ]
    assert job["unserved_nodes"] == [] and job["unserved_demand"] == 0.0
    assert job["component_nodes"] == ["S", "A", "B"]
    assert job["component_pipes"] == ["P1", "P2"]
    assert job["source_only"] is False
    assert job["source_supply"] == pytest.approx(5.0)
    assert job["total_demand"] == pytest.approx(5.0)
    assert job["solution"]["converged"] is True
    assert job["solution"]["heads"]["B"] == pytest.approx(16.0, abs=1e-9)
    # 读取同一作业：快照与创建响应一致（同一版本的整体数据）
    assert _get(job["job_id"]) == job


def test_create_job_allow_unserved_normalized_to_entry_order():
    p = _tree_payload()
    p["allow_unserved"] = ["B", "A"]  # 故意乱序：一律按节点录入顺序归并
    job = _create(p)
    assert job["allowed_unserved"] == ["A", "B"]


def test_create_job_initial_under_pressure_rejected():
    p = _tree_payload()
    p["min_head"] = 40.0  # 基线 H_A=25、H_B=16，全部欠压
    resp = client.post("/api/isolation-jobs", json=p)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "不创建停管隔离作业" in err
    assert "“A”" in err and "“B”" in err
    assert "40" in err


def test_create_job_initial_not_converged_rejected(monkeypatch):
    def boom(net):
        raise SolverNotConverged("模拟初始求解不收敛")

    monkeypatch.setattr(iso, "solve_network", boom)
    resp = client.post("/api/isolation-jobs", json=_tree_payload())
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "未能收敛" in err and "不创建停管隔离作业" in err
    assert "模拟初始求解不收敛" in err


def test_create_job_reuses_network_validation():
    bad = _tree_payload()
    bad["pipes"][0]["r"] = 0
    resp = client.post("/api/isolation-jobs", json=bad)
    assert resp.status_code == 422
    assert "阻力" in resp.json()["error"]

    disconnected = _tree_payload()
    disconnected["nodes"].append({"id": "Z", "type": "demand", "demand": 1.0})
    resp = client.post("/api/isolation-jobs", json=disconnected)
    assert resp.status_code == 422
    assert "连通" in resp.json()["error"]


def test_create_job_allow_unserved_validation():
    def expect_error(mutate, fragment):
        p = _tree_payload()
        mutate(p)
        resp = client.post("/api/isolation-jobs", json=p)
        assert resp.status_code == 422
        assert fragment in resp.json()["error"]

    expect_error(lambda p: p.update(allow_unserved="A"), "数组")
    expect_error(lambda p: p.update(allow_unserved=["S"]), "用水节点")
    expect_error(lambda p: p.update(allow_unserved=["ZZ"]), "用水节点")
    expect_error(lambda p: p.update(allow_unserved=["A", "A"]), "互异")
    expect_error(lambda p: p.update(allow_unserved=[{"id": "A"}]), "非空字符串")


def test_create_job_empty_body_and_nan():
    resp = client.post("/api/isolation-jobs", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    body = ('{"nodes":[{"id":"S","type":"source","head":NaN},'
            '{"id":"A","type":"demand","demand":1}],'
            '"pipes":[{"id":"P1","start":"S","end":"A","r":1}],"min_head":0}')
    resp = client.post("/api/isolation-jobs", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]


# ---------------------------- 切换管段：规则 ---------------------------- #
def test_toggle_close_pipe_with_allowed_unserved():
    p = _tree_payload()
    p["allow_unserved"] = ["B"]
    job = _create(p)
    resp = _toggle(job["job_id"], 0, "P2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["toggled_pipe"] == "P2" and data["toggled_to"] == "closed"
    job1 = data["job"]
    assert job1["version"] == 1
    assert job1["closed_pipes"] == ["P2"] and job1["open_pipes"] == ["P1"]
    assert job1["pipe_states"][1]["closed"] is True
    # B 失供但在允许集合内；连通区仅剩 S、A，按原需水量求解
    assert job1["unserved_nodes"] == ["B"]
    assert job1["unserved_demand"] == pytest.approx(3.0)
    assert job1["component_nodes"] == ["S", "A"]
    assert job1["component_pipes"] == ["P1"]
    assert job1["source_only"] is False
    assert job1["source_supply"] == pytest.approx(2.0)
    assert job1["total_demand"] == pytest.approx(2.0)
    assert job1["solution"]["heads"] == {"S": 50.0, "A": 46.0}
    assert set(job1["solution"]["flows"]) == {"P1"}
    # 读取快照与切换响应完全一致（不混合不同版本）
    assert _get(job["job_id"]) == job1


def test_toggle_reopen_restores_original_demand():
    """失供节点保留原需水量：重连后恢复参与求解，供水量回到基线。"""
    p = _tree_payload()
    p["allow_unserved"] = ["B"]
    job = _create(p)
    _toggle(job["job_id"], 0, "P2")
    resp = _toggle(job["job_id"], 1, "P2")  # 再次切换 = 重开
    assert resp.status_code == 200
    assert resp.json()["toggled_to"] == "open"
    job2 = resp.json()["job"]
    assert job2["version"] == 2
    assert job2["closed_pipes"] == [] and job2["open_pipes"] == ["P1", "P2"]
    assert job2["unserved_nodes"] == []
    assert job2["source_supply"] == pytest.approx(5.0)
    assert job2["total_demand"] == pytest.approx(5.0)
    assert job2["solution"]["heads"]["B"] == pytest.approx(16.0, abs=1e-9)


def test_toggle_unserved_outside_allowed_rejected_and_unchanged():
    job = _create(_tree_payload())  # 允许集合为空
    resp = _toggle(job["job_id"], 0, "P2")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "“B”" in err and "失供" in err and "不在允许暂时失供" in err
    assert "保持原状" in err
    # 关闭集合、版本、稳态结果全部保持原状
    got = _get(job["job_id"])
    assert got["version"] == 0
    assert got["closed_pipes"] == []
    assert got["unserved_nodes"] == []
    assert got["source_supply"] == pytest.approx(5.0)


def test_toggle_under_pressure_rejected_then_legal_toggle_still_works():
    """欠压拒绝不消耗版本：同一版本号的合法切换随后仍可成功。"""
    job = _create(_loop_payload())
    resp = _toggle(job["job_id"], 0, "P1")  # 关 P1 后 B 点水头 -370，欠压
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "低于最低水头限值" in err and "“B”" in err and "保持原状" in err
    got = _get(job["job_id"])
    assert got["version"] == 0 and got["closed_pipes"] == []
    # 版本未被消耗：用版本 0 关 P3（合法）成功，版本 +1
    resp = _toggle(job["job_id"], 0, "P3")
    assert resp.status_code == 200
    job1 = resp.json()["job"]
    assert job1["version"] == 1
    assert job1["closed_pipes"] == ["P3"]
    assert job1["unserved_nodes"] == []
    assert job1["component_nodes"] == ["S", "A", "B"]
    assert job1["solution"]["heads"]["A"] == pytest.approx(26.0, abs=1e-9)
    assert job1["solution"]["heads"]["B"] == pytest.approx(25.0, abs=1e-9)


def test_toggle_source_only_component_supply_zero():
    """连通区仅剩水源：供水量为零，不再调用求解器，失供节点保留原需水量。"""
    p = _tree_payload()
    p["allow_unserved"] = ["A", "B"]
    job = _create(p)
    resp = _toggle(job["job_id"], 0, "P1")
    assert resp.status_code == 200
    job1 = resp.json()["job"]
    assert job1["version"] == 1
    assert job1["source_only"] is True
    assert job1["component_nodes"] == ["S"]
    assert job1["component_pipes"] == []
    assert job1["unserved_nodes"] == ["A", "B"]
    assert job1["unserved_demand"] == pytest.approx(5.0)
    assert job1["source_supply"] == pytest.approx(0.0)
    assert job1["total_demand"] == pytest.approx(0.0)
    assert job1["solution"]["heads"] == {"S": 50.0}
    assert job1["solution"]["flows"] == {}
    assert job1["solution"]["source_supply"] == pytest.approx(0.0)
    # 重开后全部恢复
    resp = _toggle(job["job_id"], 1, "P1")
    assert resp.status_code == 200
    job2 = resp.json()["job"]
    assert job2["version"] == 2
    assert job2["source_only"] is False
    assert job2["source_supply"] == pytest.approx(5.0)
    assert job2["unserved_nodes"] == []


def test_toggle_not_converged_keeps_state(monkeypatch):
    """切换后连通区求解不收敛：全部保持原状并说明原因。"""
    real_solve = iso.solve_network
    calls = {"n": 0}

    def flaky(net):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第 1 次为创建时的初始求解
            raise SolverNotConverged("模拟切换后不收敛")
        return real_solve(net)

    monkeypatch.setattr(iso, "solve_network", flaky)
    job = _create(_loop_payload())
    resp = _toggle(job["job_id"], 0, "P3")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "未能收敛" in err and "保持原状" in err and "模拟切换后不收敛" in err
    got = _get(job["job_id"])
    assert got["version"] == 0
    assert got["closed_pipes"] == []
    assert got["source_supply"] == pytest.approx(2.0)


# ---------------------------- 版本与并发 ---------------------------- #
def test_toggle_stale_version_conflict_409():
    job = _create(_loop_payload())
    assert _toggle(job["job_id"], 0, "P3").status_code == 200  # v0 -> v1
    resp = _toggle(job["job_id"], 0, "P3")  # 旧版本重试
    assert resp.status_code == 409
    data = resp.json()
    assert data["ok"] is False
    assert "版本冲突" in data["error"]
    assert data["current_version"] == 1
    resp = _toggle(job["job_id"], 5, "P3")  # 不存在的未来版本
    assert resp.status_code == 409
    # 冲突不影响状态：仍是版本 1、P3 关闭
    got = _get(job["job_id"])
    assert got["version"] == 1 and got["closed_pipes"] == ["P3"]
    # 用当前版本重开 P3 成功
    resp = _toggle(job["job_id"], 1, "P3")
    assert resp.status_code == 200
    assert resp.json()["job"]["version"] == 2
    assert resp.json()["job"]["closed_pipes"] == []


def test_concurrent_same_version_at_most_one_success():
    """同一版本并发切换：8 个线程中恰好 1 个成功，其余全部版本冲突。"""
    snap = create_job(_loop_payload())
    job_id = snap["job_id"]
    outcomes = []
    outcomes_lock = threading.Lock()

    def worker():
        try:
            toggle_pipe(job_id, {"version": 0, "pipe_id": "P3"})
            outcome = "ok"
        except VersionConflictError:
            outcome = "conflict"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 7
    assert get_snapshot(job_id)["version"] == 1
    assert get_snapshot(job_id)["closed_pipes"] == ["P3"]


def test_jobs_are_independent():
    job_a = _create(_loop_payload())
    job_b = _create(_tree_payload())
    assert job_a["job_id"] != job_b["job_id"]
    assert _toggle(job_a["job_id"], 0, "P3").status_code == 200
    # 另一个作业不受任何影响
    got_b = _get(job_b["job_id"])
    assert got_b["version"] == 0 and got_b["closed_pipes"] == []
    assert got_b["source_supply"] == pytest.approx(5.0)


# ---------------------------- 非法操作与 404 ---------------------------- #
def test_toggle_unknown_pipe_rejected():
    job = _create(_loop_payload())
    resp = _toggle(job["job_id"], 0, "PX")
    assert resp.status_code == 422
    assert "不是该作业管网中已定义的管段" in resp.json()["error"]
    assert _get(job["job_id"])["version"] == 0


def test_toggle_bad_payload_rejected():
    job = _create(_loop_payload())
    jid = job["job_id"]
    # 缺 version / 缺 pipe_id / 版本类型错误 / 版本为负 / pipe_id 为空
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"pipe_id": "P3"}).status_code == 422
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0}).status_code == 422
    resp = client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": "0", "pipe_id": "P3"})
    assert resp.status_code == 422 and "非负整数" in resp.json()["error"]
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0.0, "pipe_id": "P3"}).status_code == 422
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": -1, "pipe_id": "P3"}).status_code == 422
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0, "pipe_id": "  "}).status_code == 422
    # 请求体不是对象 / 空请求体
    assert client.post(f"/api/isolation-jobs/{jid}/toggle",
                       json=[1, 2]).status_code == 422
    resp = client.post(f"/api/isolation-jobs/{jid}/toggle", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    # 全部失败后状态不变
    assert _get(jid)["version"] == 0


def test_unknown_job_returns_404():
    resp = client.get("/api/isolation-jobs/does-not-exist")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]
    assert "重启" in resp.json()["error"]
    resp = client.post("/api/isolation-jobs/does-not-exist/toggle",
                       json={"version": 0, "pipe_id": "P1"})
    assert resp.status_code == 404


def test_read_snapshot_never_mixes_versions():
    """连续多次成功切换后，读取到的快照内部字段始终同属一个版本。"""
    p = _tree_payload()
    p["allow_unserved"] = ["A", "B"]
    job = _create(p)
    jid = job["job_id"]
    expected_closed: list[str] = []
    for step, pipe in enumerate(["P1", "P2", "P1"], start=1):
        resp = _toggle(jid, step - 1, pipe)
        assert resp.status_code == 200
        expected_closed = resp.json()["job"]["closed_pipes"]
        got = _get(jid)
        # 版本号、关闭集合、逐管开闭、稳态结果必须互相对应
        assert got["version"] == step
        assert got["closed_pipes"] == expected_closed
        assert [ps["id"] for ps in got["pipe_states"] if ps["closed"]] \
            == expected_closed
        assert set(got["solution"]["flows"]) == set(got["component_pipes"])
        assert got["source_supply"] == pytest.approx(
            got["solution"]["source_supply"])
    # 最终状态：P1 重开、P2 仍关，B 失供
    final = _get(jid)
    assert final["version"] == 3
    assert final["closed_pipes"] == ["P2"]
    assert final["unserved_nodes"] == ["B"]
    assert final["unserved_demand"] == pytest.approx(3.0)
