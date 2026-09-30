"""求解器与 API 的端到端测试（pytest）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.solver import (
    CHECK_TOL,
    SolverNotConverged,
    ValidationError,
    analyze_pipe_outages,
    locate_single_leak,
    plan_resupply,
    solve_network,
    validate_leak_payload,
    validate_payload,
    validate_resupply_payload,
)

client = TestClient(app)


def base_loop():
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
        "min_head": 10.0,
    }


def test_single_pipe_closed_form():
    """单管：q = 需水量，解析解 H_A = H_S - r·q|q|。"""
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 2.0},
        ],
        "pipes": [{"id": "P1", "start": "S", "end": "A", "r": 1.0}],
        "min_head": 0.0,
    }
    res = solve_network(validate_payload(payload))
    assert res["source_supply"] == pytest.approx(2.0, abs=1e-12)
    assert res["heads"]["A"] == pytest.approx(26.0, abs=1e-9)
    assert res["flows"]["P1"] == pytest.approx(2.0, abs=1e-12)
    assert res["max_node_residual"] < CHECK_TOL
    assert res["max_pipe_residual"] < CHECK_TOL


def test_reversed_pipe_has_negative_flow():
    """反向录入时带符号流量为负，物理流向仍为 S→A。"""
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 2.0},
        ],
        "pipes": [{"id": "P1", "start": "A", "end": "S", "r": 1.0}],
        "min_head": 0.0,
    }
    res = solve_network(validate_payload(payload))
    assert res["flows"]["P1"] < 0
    assert res["pipe_results"][0]["direction"] == "S → A"
    assert res["heads"]["A"] == pytest.approx(26.0, abs=1e-9)


def test_loop_network_balance_and_head_law():
    res = solve_network(validate_payload(base_loop()))
    assert res["converged"] is True
    assert res["source_supply"] == pytest.approx(3.0, abs=1e-9)
    assert abs(res["balance_residual"]) < CHECK_TOL

    net = validate_payload(base_loop())
    idx = {n["id"]: i for i, n in enumerate(net.nodes)}
    H, q, r = res["heads"], res["flows"], {p["id"]: p["r"] for p in net.pipes}
    # 独立复核每根管道 H起-H终 = r·q|q|
    for p in net.pipes:
        assert H[p["start"]] - H[p["end"]] == pytest.approx(
            r[p["id"]] * q[p["id"]] * abs(q[p["id"]]), abs=CHECK_TOL)
    # 独立复核每个用水节点净流入 = 需水量
    for k, node in enumerate(net.nodes):
        if node["type"] == "demand":
            inflow = 0.0
            for p in net.pipes:
                if p["start"] == node["id"]:
                    inflow -= q[p["id"]]
                elif p["end"] == node["id"]:
                    inflow += q[p["id"]]
            assert inflow == pytest.approx(node["demand"], abs=CHECK_TOL)


def test_below_min_head_flag():
    payload = base_loop()
    payload["min_head"] = 29.0  # 所有用水节点水头约 28.7
    res = solve_network(validate_payload(payload))
    assert set(res["below_min_head"]) == {"A", "B", "C"}
    assert all(n["below_min_head"] for n in res["node_results"] if n["id"] != "S")


def test_zero_demand_nodes_allowed():
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 0.0},
            {"id": "B", "type": "demand", "demand": 3.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 2.0},
        ],
        "min_head": 0.0,
    }
    res = solve_network(validate_payload(payload))
    assert res["source_supply"] == pytest.approx(3.0, abs=1e-9)
    # B 点水头 = 30 - 1·9 - 2·9 = 3
    assert res["heads"]["B"] == pytest.approx(3.0, abs=1e-8)


# ---------------------------- 校验类用例 ---------------------------- #
@pytest.mark.parametrize("mutate,expected", [
    (lambda p: p["nodes"].__setitem__(1, {"id": "S", "type": "source", "head": 1}),
     "重复"),
    (lambda p: p["nodes"].__setitem__(1, {"id": "X", "type": "tank", "head": 1}),
     "类型"),
    (lambda p: p["nodes"].__setitem__(0, {"id": "S", "type": "source", "head": float("nan")}),
     "水头"),
    (lambda p: p["nodes"].__setitem__(1, {"id": "A", "type": "demand", "demand": -1}),
     "需水量"),
    (lambda p: p["pipes"].append({"id": "PX", "start": "S", "end": "A", "r": 1}),
     "重复"),
    (lambda p: p["pipes"][0].__setitem__("r", 0), "阻力"),
    (lambda p: p["pipes"][0].__setitem__("r", -2), "阻力"),
    (lambda p: p["pipes"].append({"id": "P9", "start": "S", "end": "S", "r": 1}),
     "自环"),
    (lambda p: p["pipes"].append({"id": "P9", "start": "S", "end": "ZZ", "r": 1}),
     "不是已定义"),
    (lambda p: p["nodes"].__setitem__(0, {"id": "S", "type": "demand", "demand": 1}),
     "水源"),
])
def test_validation_errors(mutate, expected):
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
        ],
        "pipes": [{"id": "P1", "start": "S", "end": "A", "r": 1.0}],
        "min_head": 0.0,
    }
    mutate(payload)
    with pytest.raises(ValidationError) as exc:
        validate_payload(payload)
    assert expected in str(exc.value)


def test_node_and_pipe_count_limits():
    # 11 个节点
    nodes = [{"id": "S", "type": "source", "head": 1.0}]
    nodes += [{"id": f"N{i}", "type": "demand", "demand": 1.0} for i in range(10)]
    with pytest.raises(ValidationError, match="节点数"):
        validate_payload({"nodes": nodes, "pipes": [], "min_head": 0})

    # 0 根管段
    with pytest.raises(ValidationError, match="管段数"):
        validate_payload({
            "nodes": [
                {"id": "S", "type": "source", "head": 1.0},
                {"id": "A", "type": "demand", "demand": 1.0},
            ],
            "pipes": [], "min_head": 0,
        })


def test_disconnected_graph_rejected():
    payload = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
        ],
        "pipes": [{"id": "P1", "start": "S", "end": "A", "r": 1.0}],
        "min_head": 0.0,
    }
    with pytest.raises(ValidationError, match="连通"):
        validate_payload(payload)


def test_non_finite_values_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        payload = {
            "nodes": [
                {"id": "S", "type": "source", "head": bad},
                {"id": "A", "type": "demand", "demand": 1.0},
            ],
            "pipes": [{"id": "P1", "start": "S", "end": "A", "r": 1.0}],
            "min_head": 0.0,
        }
        with pytest.raises(ValidationError, match="有限"):
            validate_payload(payload)


# ---------------------------- API 用例 ---------------------------- #
def test_api_solve_ok():
    resp = client.post("/api/solve", json=base_loop())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    r = data["result"]
    assert r["source_supply"] == pytest.approx(3.0, abs=1e-9)
    assert len(r["pipe_results"]) == 5


def test_api_validation_error_message():
    payload = base_loop()
    payload["pipes"][0]["r"] = -1
    resp = client.post("/api/solve", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert data["ok"] is False and "阻力" in data["error"]


def test_api_rejects_nan_token():
    body = '{"nodes":[{"id":"S","type":"source","head":NaN},' \
           '{"id":"A","type":"demand","demand":1}],' \
           '"pipes":[{"id":"P1","start":"S","end":"A","r":1}],"min_head":0}'
    resp = client.post("/api/solve", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]


def test_api_empty_body():
    resp = client.post("/api/solve", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_static_index_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "闭环供水管网" in resp.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"


# ---------------------------- 随机网络数值稳健性 ---------------------------- #
def test_random_connected_networks():
    rng = np.random.default_rng(0)
    n_ok = 0
    for trial in range(60):
        n = int(rng.integers(2, 11))
        edges = {tuple(sorted((i, int(rng.integers(0, i))))) for i in range(1, n)}
        target = int(rng.integers(len(edges), min(16, n * (n - 1) // 2) + 1))
        possible = list({(i, j) for i in range(n) for j in range(i + 1, n)} - edges)
        extra = min(target - len(edges), len(possible))
        perm = rng.permutation(len(possible))[:extra]
        edges.update(possible[k] for k in perm)
        src = int(rng.integers(0, n))
        nodes = [
            {"id": f"N{i}", "type": "source",
             "head": float(rng.uniform(20, 100))} if i == src
            else {"id": f"N{i}", "type": "demand",
                  "demand": float(rng.uniform(0, 5))}
            for i in range(n)
        ]
        pipes = [
            {"id": f"P{k}", "start": f"N{i}", "end": f"N{j}",
             "r": float(10 ** rng.uniform(-3, 3))}
            for k, (i, j) in enumerate(edges, 1)
        ]
        res = solve_network(validate_payload(
            {"nodes": nodes, "pipes": pipes, "min_head": -1e9}))
        assert res["max_node_residual"] < CHECK_TOL
        assert res["max_pipe_residual"] < CHECK_TOL
        assert math.isclose(res["source_supply"], res["total_demand"], abs_tol=CHECK_TOL)
        n_ok += 1
    assert n_ok == 60


# ---------------------------- 逐根停管分析 ---------------------------- #
def _tree_payload():
    """树状网：每根管段都是桥，移除必有失供。

        S --P1-- A(需 2) --P2-- B(需 3) --P3-- C(需 4)
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 50.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 3.0},
            {"id": "C", "type": "demand", "demand": 4.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "C", "r": 1.0},
        ],
        "min_head": 0.0,
    }


def test_outage_tree_all_disconnected_sorted_by_unserved_demand():
    a = analyze_pipe_outages(validate_payload(_tree_payload()))
    stats = [(s["pipe_id"], s["status"]) for s in a["scenarios"]]
    assert stats == [
        ("P1", "disconnected"),  # 失供 2+3+4=9
        ("P2", "disconnected"),  # 失供 3+4=7
        ("P3", "disconnected"),  # 失供 4
    ]
    first = a["scenarios"][0]
    assert first["unserved_nodes"] == ["A", "B", "C"]
    assert first["unserved_demand"] == pytest.approx(9.0)
    assert "min_demand_head" not in first  # 断联情景不生成水头
    assert a["scenarios"][1]["unserved_nodes"] == ["B", "C"]
    assert a["scenarios"][1]["unserved_demand"] == pytest.approx(7.0)
    assert a["scenarios"][2]["unserved_nodes"] == ["C"]
    assert [s["rank"] for s in a["scenarios"]] == [1, 2, 3]
    assert a["counts"] == {"disconnected": 3, "connected": 0, "not_converged": 0}


def _loop_branch_payload():
    """环 + 两根枝：P4、P5 为桥，P1～P3 构成 S-A-B-S 环。

        S ──P1── A(需1) ──P2── B(需1) ──P4── D(需5)
         │                  │                    （挂枝）
         └──────P3──────────┘
        A ──P5── E(需2)（挂枝）
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 40.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "D", "type": "demand", "demand": 5.0},
            {"id": "E", "type": "demand", "demand": 2.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "S", "r": 1.0},
            {"id": "P4", "start": "B", "end": "D", "r": 1.0},
            {"id": "P5", "start": "A", "end": "E", "r": 1.0},
        ],
        "min_head": 0.0,
    }


def test_outage_mixed_grouping_and_connected_fields():
    payload = _loop_branch_payload()
    a = analyze_pipe_outages(validate_payload(payload))
    statuses = [(s["pipe_id"], s["status"]) for s in a["scenarios"]]
    # 断联情景在前，按失供量降序：P4 失供 D(5) > P5 失供 E(2)
    assert statuses[:2] == [("P4", "disconnected"), ("P5", "disconnected")]
    # 随后是三个连通情景，按最低用水节点水头升序
    connected = [s for s in a["scenarios"] if s["status"] == "connected"]
    assert len(connected) == 3
    # 失去 S-B 直通管 P3 时最不利，其次失去 S-A 管 P1，最后失去环中段 P2
    assert [s["pipe_id"] for s in connected] == ["P3", "P1", "P2"]
    heads = [s["min_demand_head"] for s in connected]
    assert heads == sorted(heads)

    p4 = a["scenarios"][0]
    assert p4["unserved_nodes"] == ["D"]
    assert p4["unserved_demand"] == pytest.approx(5.0)

    # 连通情景字段完整，且与独立重解一致
    net = validate_payload(payload)
    for s in connected:
        idx = next(i for i, p in enumerate(net.pipes) if p["id"] == s["pipe_id"])
        sub = type(net)(nodes=net.nodes,
                        pipes=net.pipes[:idx] + net.pipes[idx + 1:],
                        min_head=net.min_head)
        res = solve_network(sub)
        demand_ids = [n["id"] for n in net.nodes if n["type"] == "demand"]
        assert s["min_demand_head"] == pytest.approx(
            min(res["heads"][nid] for nid in demand_ids), abs=1e-9)
        assert s["below_min_head"] == res["below_min_head"]
        assert s["solution"]["source_supply"] == pytest.approx(
            res["source_supply"], abs=1e-9)

    # 基线与各情景共用同一限值；baseline 中低水头节点集合随分析返回
    assert a["baseline"]["converged"] is True
    assert set(a["baseline_below_min_head"]) == set(a["baseline"]["below_min_head"])


def test_outage_new_below_min_head_nodes():
    """选择使基线合格、但移除 P1 后 B 点水头跌破限值的阈值。"""
    payload = _loop_branch_payload()
    net = validate_payload(payload)
    baseline = solve_network(net)
    # 找到一个移除后最低水头下降最多的环管，以其最低水头与基线最低水头之间取限值
    a0 = analyze_pipe_outages(net)
    worst = min((s for s in a0["scenarios"] if s["status"] == "connected"),
                key=lambda s: s["min_demand_head"])
    threshold = (worst["min_demand_head"] +
                 min(baseline["heads"][n["id"]]
                     for n in net.nodes if n["type"] == "demand")) / 2
    payload["min_head"] = threshold
    a = analyze_pipe_outages(validate_payload(payload))
    base_below = set(a["baseline_below_min_head"])
    assert base_below == set()  # 限值取在两者之间，基线应全部合格
    target = next(s for s in a["scenarios"] if s["pipe_id"] == worst["pipe_id"])
    assert target["new_below_min_head"], "最差情景应出现比基线新增的低水头节点"
    assert set(target["new_below_min_head"]).issubset(set(target["below_min_head"]))
    # 其余连通情景的“新增”集合 = 情景低水头集合 − 基线低水头集合
    for s in a["scenarios"]:
        if s["status"] == "connected":
            assert s["new_below_min_head"] == [
                nid for nid in s["below_min_head"] if nid not in base_below]


def test_outage_tie_keeps_pipe_entry_order():
    """同分情景保持管段录入顺序：base_loop 关于 S-B 对角线镜像对称，
    移除 P1（S-A）与 P4（C-S）最低水头相同；移除 P2（A-B）与 P3（B-C）亦相同。"""
    payload = base_loop()
    a = analyze_pipe_outages(validate_payload(payload))
    # 无断联（原图连通，单管移除后仍连通）
    assert a["counts"]["disconnected"] == 0
    ordered = a["scenarios"]
    by_id = {s["pipe_id"]: s for s in ordered}
    assert by_id["P1"]["min_demand_head"] == pytest.approx(
        by_id["P4"]["min_demand_head"], abs=1e-9)
    assert by_id["P2"]["min_demand_head"] == pytest.approx(
        by_id["P3"]["min_demand_head"], abs=1e-9)
    # 录入顺序相同的 P1 排在 P4 前、P2 排在 P3 前
    assert by_id["P1"]["rank"] < by_id["P4"]["rank"]
    assert by_id["P2"]["rank"] < by_id["P3"]["rank"]
    # 整体仍按最低水头非降序
    hs = [s["min_demand_head"] for s in ordered]
    assert hs == sorted(hs)
    assert [s["rank"] for s in ordered] == list(range(1, len(ordered) + 1))


def test_outage_single_nonconverged_scenario_marked_and_last(monkeypatch):
    """某一情景单独不收敛：标明并置于最后，其余情景照常给出。"""
    payload = _loop_branch_payload()
    real_solve = solve_network
    call = {"n": 0}

    def fake_solve(net):
        # analyze_pipe_outages 的第 2 次求解 = 移除第 0 根管（P1）的情景
        call["n"] += 1
        if call["n"] == 2:
            raise SolverNotConverged("模拟 P1 情景不收敛")
        return real_solve(net)

    monkeypatch.setattr("app.solver.solve_network", fake_solve)
    a = analyze_pipe_outages(validate_payload(payload))
    assert a["counts"] == {"disconnected": 2, "connected": 2, "not_converged": 1}
    last = a["scenarios"][-1]
    assert last["status"] == "not_converged"
    assert last["pipe_id"] == "P1"
    assert "模拟 P1 情景不收敛" in last["error"]
    # 断联在前、连通居中、不收敛最后
    order = [s["status"] for s in a["scenarios"]]
    assert order == ["disconnected", "disconnected", "connected", "connected",
                     "not_converged"]
    assert all("min_demand_head" in s for s in a["scenarios"]
               if s["status"] == "connected")


# ---------------------------- 停管分析 API ---------------------------- #
def test_api_analyze_outages_ok():
    resp = client.post("/api/analyze-outages", json=_tree_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    a = data["analysis"]
    assert [s["pipe_id"] for s in a["scenarios"]] == ["P1", "P2", "P3"]
    assert a["scenarios"][0]["unserved_demand"] == pytest.approx(9.0)
    assert a["baseline"]["converged"] is True


def test_api_analyze_outages_validation_error():
    payload = _tree_payload()
    payload["pipes"][0]["r"] = 0
    resp = client.post("/api/analyze-outages", json=payload)
    assert resp.status_code == 422
    assert "阻力" in resp.json()["error"]


def test_api_analyze_outages_empty_body():
    resp = client.post("/api/analyze-outages", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


# ---------------------------- 单点漏损定位 ---------------------------- #
def _leak_payload():
    """与 base_loop 相同的 4 节点环网（S-A-B-C-S + 对角线 S-B）。"""
    p = base_loop()
    p["leak_upper_bound"] = 2.0
    return p


def _truth_heads(payload: dict, leak_node: str, leak: float) -> dict[str, float]:
    """在给定节点需水量上增加 leak 后做一次“实测真值”求解。"""
    net = validate_payload(payload)
    bumped = [dict(n) for n in net.nodes]
    for n in bumped:
        if n["id"] == leak_node:
            n["demand"] += leak
    truth = solve_network(type(net)(nodes=bumped, pipes=net.pipes,
                                    min_head=net.min_head))
    return truth["heads"]


def test_leak_locates_true_node_and_recovers_amount():
    """真值：B 点额外漏 0.8，测点 A、C；最优候选必须是 B 且漏量约 0.8。"""
    p = _leak_payload()
    truth = _truth_heads(p, "B", 0.8)
    p["measurements"] = [
        {"node": "A", "head": truth["A"]},
        {"node": "C", "head": truth["C"]},
    ]
    a = locate_single_leak(validate_leak_payload(p))

    assert a["leak_detected"] is True
    assert a["leak_node"] == "B"
    best = a["candidates"][0]
    assert best["node_id"] == "B"
    assert best["leak"] == pytest.approx(0.8, abs=1e-8)
    assert best["rmse"] < 1e-9
    assert a["baseline_rmse"] > 1e-6  # 无漏损基线无法解释实测水头
    # 预测水头即拟合解在测点上的水头，与实测基本吻合
    assert best["predicted_heads"]["A"] == pytest.approx(truth["A"], abs=1e-8)
    assert best["predicted_heads"]["C"] == pytest.approx(truth["C"], abs=1e-8)
    assert abs(best["head_errors"]["A"]) < 1e-8
    # 每个用水节点一个候选且带排名，排序按 RMSE 非降，同分保持录入顺序
    assert [c["node_id"] for c in a["candidates"]] == ["B", "A", "C"]
    rmses = [c["rmse"] for c in a["candidates"]]
    assert rmses == sorted(rmses)
    assert [c["rank"] for c in a["candidates"]] == [1, 2, 3]
    assert a["counts"] == {"fitted": 3, "not_converged": 0}
    assert a["indistinguishable"] is False
    assert a["indistinguishable_nodes"] == ["B"]


def test_leak_no_leak_baseline_zero_rmse_not_detected():
    """实测水头恰好等于无漏损基线时，最优漏量为 0，不判定存在漏损。"""
    p = _leak_payload()
    base = solve_network(validate_payload(p))
    p["measurements"] = [
        {"node": "A", "head": base["heads"]["A"]},
        {"node": "C", "head": base["heads"]["C"]},
    ]
    a = locate_single_leak(validate_leak_payload(p))
    assert a["baseline_rmse"] == pytest.approx(0.0, abs=1e-12)
    assert a["leak_detected"] is False
    assert a["leak_node"] is None
    # 三个候选在 0 漏量处都能完美拟合，构成无法区分（但仍不判定漏损）
    assert a["indistinguishable"] is True
    assert set(a["indistinguishable_nodes"]) == {"A", "B", "C"}
    assert all(c["leak"] == 0.0 for c in a["candidates"])
    assert "不判定存在漏损" in a["conclusion"]


def test_leak_indistinguishable_symmetric_tree():
    """对称挂枝网：B 直接挂水源侧，A、C 为对称枝；在 A 加漏 0.5，测点 B、D，
    B/A/C 三节点拟合的漏量与 RMSE 相同，按录入顺序 B 居首但标为无法区分。"""
    p = {
        "nodes": [
            {"id": "S", "type": "source", "head": 40.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "D", "type": "demand", "demand": 1.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "C", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "B", "r": 1.0},
            {"id": "P2", "start": "B", "end": "D", "r": 1.0},
            {"id": "P3", "start": "B", "end": "A", "r": 1.0},
            {"id": "P4", "start": "B", "end": "C", "r": 1.0},
        ],
        "min_head": 0.0,
        "leak_upper_bound": 2.0,
    }
    truth = _truth_heads(p, "A", 0.5)
    p["measurements"] = [
        {"node": "B", "head": truth["B"]},
        {"node": "D", "head": truth["D"]},
    ]
    a = locate_single_leak(validate_leak_payload(p))
    top3 = a["candidates"][:3]
    assert [c["node_id"] for c in top3] == ["B", "A", "C"]
    assert all(c["leak"] == pytest.approx(0.5, abs=1e-8) for c in top3)
    assert all(c["rmse"] == pytest.approx(top3[0]["rmse"], abs=1e-12) for c in top3)
    assert a["indistinguishable"] is True
    assert set(a["indistinguishable_nodes"]) == {"A", "B", "C"}
    # 同分按录入顺序，最优节点为录入最早的 B
    assert a["leak_node"] == "B"
    assert a["leak_detected"] is True


def test_leak_endpoint_upper_must_be_compared():
    """真实漏量超过上限时，最优拟合漏量必须正好落在端点上限处（而非某个内点）。"""
    p = _leak_payload()
    p["leak_upper_bound"] = 1.0
    truth = _truth_heads(p, "B", 3.0)
    p["measurements"] = [
        {"node": "A", "head": truth["A"]},
        {"node": "C", "head": truth["C"]},
    ]
    a = locate_single_leak(validate_leak_payload(p))
    best = a["candidates"][0]
    assert best["leak"] == pytest.approx(1.0, abs=1e-12)
    assert best["rmse"] < a["baseline_rmse"]


def test_leak_validation_measurement_rules():
    """测点与上限的各类非法输入都须被拒绝并给出中文原因。"""
    def expect_error(mutate, fragment):
        p = _leak_payload()
        mutate(p)
        with pytest.raises(ValidationError) as exc:
            validate_leak_payload(p)
        assert fragment in str(exc.value)

    one = [{"node": "A", "head": 28.0}]
    nine = [{"node": nid, "head": 28.0}
            for nid in ["A", "B", "C"]] + [
        {"node": f"Z{i}", "head": 1.0} for i in range(6)]
    expect_error(lambda p: p.update(measurements=one), "2～8")
    expect_error(lambda p: p.update(measurements=nine), "2～8")
    expect_error(lambda p: p.update(measurements="x"), "measurements 必须是")
    expect_error(lambda p: p.update(measurements=[{"node": "S", "head": 30.0},
                                                  {"node": "A", "head": 28.0}]),
                 "用水节点")
    expect_error(lambda p: p.update(measurements=[{"node": "A", "head": 28.0},
                                                  {"node": "A", "head": 27.0}]),
                 "互异")
    expect_error(lambda p: p.update(measurements=[{"node": "A", "head": 28.0},
                                                  {"node": "ZZ", "head": 27.0}]),
                 "用水节点")
    expect_error(lambda p: p.update(measurements=[{"node": "A"},
                                                  {"node": "B", "head": 27.0}]),
                 "实测水头")
    expect_error(lambda p: p.update(measurements=[{"node": "A", "head": float("inf")},
                                                  {"node": "B", "head": 27.0}]),
                 "有限")
    valid_meas = [{"node": "A", "head": 28.0}, {"node": "C", "head": 27.0}]
    expect_error(lambda p: p.update(measurements=valid_meas, leak_upper_bound=0.0),
                 "正数")
    expect_error(lambda p: p.update(measurements=valid_meas, leak_upper_bound=-1.0),
                 "正数")
    expect_error(lambda p: p.update(measurements=valid_meas,
                                    leak_upper_bound=float("nan")),
                 "有限")


def test_leak_baseline_nonconvergence_propagates(monkeypatch):
    """无漏损基线不收敛时整体报错（SolverNotConverged），由 API 转 422。"""
    p = _leak_payload()
    p["measurements"] = [{"node": "A", "head": 28.0}, {"node": "C", "head": 27.0}]

    def boom(net):
        raise SolverNotConverged("模拟基线不收敛")

    monkeypatch.setattr("app.solver.solve_network", boom)
    with pytest.raises(SolverNotConverged, match="基线"):
        locate_single_leak(validate_leak_payload(p))


def test_leak_single_candidate_failure_marked_and_last(monkeypatch):
    """单个候选（B）所有非零漏量点均不收敛：标明 not_converged 排最后，其余照常。"""
    p = {
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
        "leak_upper_bound": 2.0,
        "measurements": [{"node": "A", "head": 27.0}, {"node": "C", "head": 20.0}],
    }
    real_solve = solve_network

    def selective(net):
        extra_b = next(n["demand"] for n in net.nodes if n["id"] == "B") - 1.0
        if extra_b > 1e-12:
            raise SolverNotConverged("模拟 B 候选不收敛")
        return real_solve(net)

    monkeypatch.setattr("app.solver.solve_network", selective)
    a = locate_single_leak(validate_leak_payload(p))
    assert a["counts"] == {"fitted": 2, "not_converged": 1}
    failed = [c for c in a["candidates"] if c["status"] == "not_converged"]
    assert [c["node_id"] for c in failed] == ["B"]
    assert "未能收敛" in failed[0]["error"]
    assert a["candidates"][-1]["node_id"] == "B"
    assert "rmse" not in a["candidates"][-1]
    # 成功候选仍然给出完整拟合字段
    for c in a["candidates"][:-1]:
        assert c["status"] == "fitted"
        assert set(c["predicted_heads"]) == {"A", "C"}
        assert c["rmse"] >= 0.0


# ---------------------------- 单点漏损 API ---------------------------- #
def test_api_locate_leak_ok():
    p = _leak_payload()
    truth = _truth_heads(p, "B", 0.8)
    p["measurements"] = [
        {"node": "A", "head": truth["A"]},
        {"node": "C", "head": truth["C"]},
    ]
    resp = client.post("/api/locate-leak", json=p)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    a = data["leak_analysis"]
    assert a["leak_detected"] is True
    assert a["leak_node"] == "B"
    assert a["candidates"][0]["leak"] == pytest.approx(0.8, abs=1e-8)
    assert a["baseline"]["converged"] is True
    assert a["leak_upper_bound"] == 2.0
    assert [m["node"] for m in a["measurements"]] == ["A", "C"]


def test_api_locate_leak_validation_error():
    p = _leak_payload()
    p["measurements"] = [{"node": "S", "head": 30.0}, {"node": "A", "head": 28.0}]
    resp = client.post("/api/locate-leak", json=p)
    assert resp.status_code == 422
    assert "用水节点" in resp.json()["error"]


def test_api_locate_leak_missing_upper_bound():
    p = base_loop()
    p["measurements"] = [{"node": "A", "head": 28.0}, {"node": "C", "head": 27.0}]
    resp = client.post("/api/locate-leak", json=p)
    assert resp.status_code == 422
    assert "leak_upper_bound" in resp.json()["error"]


def test_api_locate_leak_empty_body():
    resp = client.post("/api/locate-leak", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_api_locate_leak_rejects_nan_token():
    body = ('{"nodes":[{"id":"S","type":"source","head":30},'
            '{"id":"A","type":"demand","demand":1}],'
            '"pipes":[{"id":"P1","start":"S","end":"A","r":1}],'
            '"min_head":0,"leak_upper_bound":1,'
            '"measurements":[{"node":"A","head":NaN}]}')
    resp = client.post("/api/locate-leak", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]


def test_api_locate_leak_baseline_nonconvergence_422(monkeypatch):
    """基线不收敛时 API 返回 422 与中文原因。"""
    import app.solver as solver_mod

    def boom(net):
        raise SolverNotConverged("模拟基线不收敛")

    monkeypatch.setattr(solver_mod, "solve_network", boom)
    p = _leak_payload()
    p["measurements"] = [{"node": "A", "head": 28.0}, {"node": "C", "head": 27.0}]
    resp = client.post("/api/locate-leak", json=p)
    assert resp.status_code == 422
    assert "基线" in resp.json()["error"]


# ---------------------------- 停供节点同时复供组合优选 ---------------------------- #
def _resupply_tree_payload():
    """树状网 S-A-B-C，候选 A/B/C 表内需水均为 2，最低水头限值 9。

    各组合最低用水节点水头（末端最不利点）：
      空集   30（候选全停，仅非候选需水 0）
      {A}    26；{B} 22；{C} 18
      {A,B}  10；{A,C} 6（C 欠压）；{B,C} -6（B、C 欠压）
      {A,B,C} -26（全欠压）
    可行组合中 {A,B} 恢复总量 4 最大（{A,C} 与 {B,C} 欠压不可选）。
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 2.0},
            {"id": "C", "type": "demand", "demand": 2.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "C", "r": 1.0},
        ],
        "min_head": 9.0,
        "resupply_nodes": ["A", "B", "C"],
    }


def _resupply_loop_payload():
    """4 节点环网（S-A-B-C-S + 对角线 S-B，r=2.5），各用水点表内需水均为 1。

    min_head=29 时三者同复供不可行（最低 ≈28.687 < 29），两两组合恢复总量
    均为 2 且可行，按节点录入顺序字典序 {A,B} 胜 {A,C}、{B,C}。
    """
    p = base_loop()
    p["min_head"] = 29.0
    p["resupply_nodes"] = ["C", "B", "A"]  # 故意乱序：组合须按节点录入顺序枚举/排序
    return p


def test_resupply_basic_optimal_selection():
    a = plan_resupply(validate_resupply_payload(_resupply_tree_payload()))
    assert a["status"] == "optimal"
    assert a["optimal_confirmed"] is True
    assert a["selected"]["nodes"] == ["A", "B"]
    assert a["restored_demand_total"] == pytest.approx(4.0)
    assert a["unrestored_nodes"] == ["C"]
    # 含空集在内共 2^3 = 8 个组合全部求解
    assert a["total_combinations"] == 8
    assert a["evaluated_combinations"] == 8
    assert a["counts"] == {"feasible": 5, "below_limit": 3, "not_converged": 0}
    # 恢复总量降序排列；feasible 在前、below_limit 随后，排名连续
    feasible = a["combinations"][:5]
    totals = [c["restored_demand"] for c in feasible]
    assert totals == sorted(totals, reverse=True)
    assert [c["rank"] for c in a["combinations"]] == list(range(1, 9))
    assert all(c["status"] == "feasible" for c in feasible)
    assert all(c["status"] == "below_limit" for c in a["combinations"][5:])
    # 选中组合的稳态解：A、B 恢复 2，C 仍按 0 需水；水源供 4，最低水头不低于 9
    sel = a["selected"]["solution"]
    assert sel["total_demand"] == pytest.approx(4.0)
    assert sel["source_supply"] == pytest.approx(4.0)
    assert sel["below_min_head"] == []
    assert a["selected"]["min_demand_head"] >= 9.0 - 1e-12
    # 基线（空集）：候选全部按 0 需水，总需水/供水量为 0，各点水头均 30
    base = a["baseline"]
    assert base["total_demand"] == pytest.approx(0.0)
    assert base["source_supply"] == pytest.approx(0.0)
    assert base["heads"] == {"S": 30.0, "A": 30.0, "B": 30.0, "C": 30.0}
    assert base["below_min_head"] == []
    # 欠压组合：{A,B,C} 恢复 6、三点全欠压；{B,C} 欠压节点为 B、C
    by_nodes = {tuple(c["nodes"]): c for c in a["combinations"]}
    full = by_nodes[("A", "B", "C")]
    assert full["status"] == "below_limit"
    assert set(full["below_min_head"]) == {"A", "B", "C"}
    assert by_nodes[("B", "C")]["below_min_head"] == ["B", "C"]
    assert by_nodes[("A", "C")]["below_min_head"] == ["C"]


def test_resupply_tie_more_nodes_then_lexicographic():
    """总量相同：先比节点数（多者胜），再按节点录入顺序的字典序。"""
    # 1) 字典序：环网各点等需水（=1），min_head=29，两两组合总量同为 2
    a = plan_resupply(validate_resupply_payload(_resupply_loop_payload()))
    assert a["status"] == "optimal"
    # 请求数组乱序不影响候选顺序：一律按节点录入顺序
    assert a["resupply_nodes"] == ["A", "B", "C"]
    pairs = [c for c in a["combinations"] if c["node_count"] == 2
             and c["status"] == "feasible"]
    assert [c["nodes"] for c in pairs] == [["A", "B"], ["A", "C"], ["B", "C"]]
    assert a["selected"]["nodes"] == ["A", "B"]
    assert a["restored_demand_total"] == pytest.approx(2.0)
    assert a["unrestored_nodes"] == ["C"]
    # 三节点全复供欠压不可选
    by_nodes = {tuple(c["nodes"]): c for c in a["combinations"]}
    assert by_nodes[("A", "B", "C")]["status"] == "below_limit"

    # 2) 节点数优先：A 需水 2、B/C 各 1；min_head=20.5 时 {A}（最低 26）
    #    与 {B,C}（最低 21）可行且总量同为 2，{B,C} 因节点更多胜出
    p = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "C", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "B", "end": "C", "r": 1.0},
        ],
        "min_head": 20.5,
        "resupply_nodes": ["A", "B", "C"],
    }
    a2 = plan_resupply(validate_resupply_payload(p))
    assert a2["selected"]["nodes"] == ["B", "C"]
    assert a2["restored_demand_total"] == pytest.approx(2.0)
    assert a2["unrestored_nodes"] == ["A"]
    by_nodes2 = {tuple(c["nodes"]): c for c in a2["combinations"]}
    assert by_nodes2[("A",)]["status"] == "feasible"
    assert a2["combinations"][0]["nodes"] == ["B", "C"]  # 同分排在 {A} 前


def test_resupply_empty_set_is_optimal_when_all_restorations_infeasible():
    """除空集外没有任何组合满足水头限值：最优为空集，恢复总量 0，全部候选未复供。"""
    p = _resupply_tree_payload()
    p["min_head"] = 29.9
    a = plan_resupply(validate_resupply_payload(p))
    assert a["status"] == "optimal"
    assert a["selected"]["nodes"] == []
    assert a["selected"]["node_count"] == 0
    assert a["restored_demand_total"] == pytest.approx(0.0)
    assert a["unrestored_nodes"] == ["A", "B", "C"]
    assert a["counts"] == {"feasible": 1, "below_limit": 7, "not_converged": 0}
    assert a["selected"]["solution"]["total_demand"] == pytest.approx(0.0)
    assert "空集" in a["conclusion"]


def test_resupply_baseline_below_limit_means_cannot_restart():
    """复供前基线即欠压：status=baseline_below_limit，不再评估其他组合，无法复供。"""
    p = _resupply_tree_payload()
    p["min_head"] = 35.0  # 基线候选全停、各点水头 30，全部欠压
    a = plan_resupply(validate_resupply_payload(p))
    assert a["status"] == "baseline_below_limit"
    assert a["optimal_confirmed"] is False
    assert a["selected"] is None
    assert a["restored_demand_total"] is None
    assert a["unrestored_nodes"] == ["A", "B", "C"]
    assert set(a["baseline_below_min_head"]) == {"A", "B", "C"}
    assert a["evaluated_combinations"] == 1  # 只算了基线
    assert a["counts"] == {"feasible": 0, "below_limit": 1, "not_converged": 0}
    assert "无法复供" in a["conclusion"]


def test_resupply_non_candidate_demand_node_keeps_its_demand():
    """非候选用水节点不参与停供/复供：基线与各组合中都保持表内需水。"""
    p = {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "D", "type": "demand", "demand": 3.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 0.01},
            {"id": "P2", "start": "A", "end": "B", "r": 0.01},
            {"id": "P3", "start": "B", "end": "D", "r": 0.01},
        ],
        "min_head": 0.0,
        "resupply_nodes": ["A", "B"],
    }
    a = plan_resupply(validate_resupply_payload(p))
    assert a["status"] == "optimal"
    assert a["selected"]["nodes"] == ["A", "B"]
    # 基线：仅非候选 D 带 3 需水，A/B 按 0
    assert a["baseline"]["total_demand"] == pytest.approx(3.0)
    # 选中组合：A、B 恢复后总需水 2+1+3=6
    assert a["selected"]["solution"]["total_demand"] == pytest.approx(6.0)
    # 候选表内需水回显不含 D
    assert set(a["table_demands"]) == {"A", "B"}
    assert a["table_demand_total"] == pytest.approx(3.0)


def test_resupply_baseline_not_converged(monkeypatch):
    """基线不收敛：status=baseline_not_converged、baseline=None、不评估其余组合，
    且不能当作欠压。"""
    p = _resupply_tree_payload()

    def boom(net):
        raise SolverNotConverged("模拟基线不收敛")

    monkeypatch.setattr("app.solver.solve_network", boom)
    a = plan_resupply(validate_resupply_payload(p))
    assert a["status"] == "baseline_not_converged"
    assert a["optimal_confirmed"] is False
    assert a["baseline"] is None
    assert "模拟基线不收敛" in a["baseline_error"]
    assert a["selected"] is None
    assert a["restored_demand_total"] is None
    assert a["evaluated_combinations"] == 0
    assert a["counts"] == {"feasible": 0, "below_limit": 0, "not_converged": 0}
    assert len(a["failed_combinations"]) == 1
    assert a["failed_combinations"][0]["nodes"] == []
    assert "无法确认最优" in a["conclusion"]


def test_resupply_combo_not_converged_marks_unconfirmed_not_underpressure(
        monkeypatch):
    """某一组合单独不收敛：标记 not_converged、无法确认最优（绝不按欠压处理），
    同时给出已确认可行组合中的临时最优；求解失败不影响其他组合。"""
    p = _resupply_tree_payload()
    real_solve = solve_network
    calls = {"n": 0}

    def selective(net):
        calls["n"] += 1
        # 第 1 次为基线（空集）；第 8 次为 {A,B,C}
        if calls["n"] == 8:
            raise SolverNotConverged("模拟全复供不收敛")
        return real_solve(net)

    monkeypatch.setattr("app.solver.solve_network", selective)
    a = plan_resupply(validate_resupply_payload(p))
    assert a["status"] == "not_confirmed"
    assert a["optimal_confirmed"] is False
    assert a["selected_provisional"] is True
    # 临时最优与无故障时一致：{A,B}，恢复 4，C 未复供
    assert a["selected"]["nodes"] == ["A", "B"]
    assert a["restored_demand_total"] == pytest.approx(4.0)
    assert a["unrestored_nodes"] == ["C"]
    assert a["counts"] == {"feasible": 5, "below_limit": 2, "not_converged": 1}
    failed = a["failed_combinations"]
    assert len(failed) == 1
    assert failed[0]["nodes"] == ["A", "B", "C"]
    assert failed[0]["restored_demand"] == pytest.approx(6.0)
    assert "模拟全复供不收敛" in failed[0]["error"]
    assert failed[0]["rank"] == 8  # 不收敛情景排最后
    assert "无法在全部组合上确认最优" in a["conclusion"]
    assert "临时最优" in a["conclusion"]
    # 失败组合不在 below_limit 统计内（求解失败 ≠ 欠压）
    for c in a["combinations"]:
        if tuple(c["nodes"]) == ("A", "B", "C"):
            assert c["status"] == "not_converged"
            assert "below_min_head" not in c


def test_resupply_validation_rules():
    """候选节点的各类非法输入都须被拒绝并给出中文原因。"""
    def expect_error(mutate, fragment):
        p = _resupply_tree_payload()
        mutate(p)
        with pytest.raises(ValidationError) as exc:
            validate_resupply_payload(p)
        assert fragment in str(exc.value)

    expect_error(lambda p: p.update(resupply_nodes=["A"]), "2～6")
    expect_error(lambda p: p.update(resupply_nodes=["A", "B", "C", "D", "E", "F", "G"]),
                 "2～6")
    expect_error(lambda p: p.update(resupply_nodes="A,B,C"), "数组")
    expect_error(lambda p: p.update(resupply_nodes=123), "数组")
    expect_error(lambda p: p.update(resupply_nodes=["S", "A"]), "用水节点")
    expect_error(lambda p: p.update(resupply_nodes=["A", "ZZ"]), "用水节点")
    expect_error(lambda p: p.update(resupply_nodes=["A", "A", "B"]), "互异")
    expect_error(lambda p: p.update(resupply_nodes=[{"id": "A"}, "B"]), "非空字符串")

    # 候选为零需水用水节点：拒绝（表内需水量必须为正）
    def zero_demand(p):
        p["nodes"][1]["demand"] = 0.0
        p["resupply_nodes"] = ["A", "B"]
    expect_error(zero_demand, "表内需水量必须为正数")

    # 候选缺失
    with pytest.raises(ValidationError, match="resupply_nodes"):
        validate_resupply_payload({k: v for k, v in
                                   _resupply_tree_payload().items()
                                   if k != "resupply_nodes"})

    # 原有管网校验仍然生效
    bad = _resupply_tree_payload()
    bad["pipes"][0]["r"] = 0
    with pytest.raises(ValidationError, match="阻力"):
        validate_resupply_payload(bad)


# ---------------------------- 复供优选 API ---------------------------- #
def test_api_plan_resupply_ok():
    resp = client.post("/api/plan-resupply", json=_resupply_tree_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    a = data["resupply_analysis"]
    assert a["status"] == "optimal"
    assert a["selected"]["nodes"] == ["A", "B"]
    assert a["restored_demand_total"] == pytest.approx(4.0)
    assert a["unrestored_nodes"] == ["C"]
    assert a["baseline"]["converged"] is True
    assert len(a["combinations"]) == 8
    assert a["selected"]["solution"]["converged"] is True


def test_api_plan_resupply_validation_error():
    p = _resupply_tree_payload()
    p["resupply_nodes"] = ["A"]  # 少于 2 个
    resp = client.post("/api/plan-resupply", json=p)
    assert resp.status_code == 422
    assert "2～6" in resp.json()["error"]


def test_api_plan_resupply_baseline_below_limit_still_200():
    """基线欠压属于业务结论（非输入错误、非求解异常），返回 200。"""
    p = _resupply_tree_payload()
    p["min_head"] = 35.0
    resp = client.post("/api/plan-resupply", json=p)
    assert resp.status_code == 200
    a = resp.json()["resupply_analysis"]
    assert a["status"] == "baseline_below_limit"
    assert a["selected"] is None
    assert "无法复供" in a["conclusion"]


def test_api_plan_resupply_baseline_nonconvergence_is_structured(monkeypatch):
    """基线不收敛属于“无法确认最优”的业务结论：返回 200 与
    status=baseline_not_converged，并附中文原因（不能当作欠压或输入错误）。"""
    import app.solver as solver_mod

    def boom(net):
        raise SolverNotConverged("模拟基线不收敛")

    monkeypatch.setattr(solver_mod, "solve_network", boom)
    resp = client.post("/api/plan-resupply", json=_resupply_tree_payload())
    assert resp.status_code == 200
    a = resp.json()["resupply_analysis"]
    assert a["status"] == "baseline_not_converged"
    assert a["optimal_confirmed"] is False
    assert a["baseline"] is None
    assert "模拟基线不收敛" in a["baseline_error"]
    assert a["selected"] is None
    assert "无法确认最优" in a["conclusion"]


def test_api_plan_resupply_empty_body():
    resp = client.post("/api/plan-resupply", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_api_plan_resupply_rejects_nan_token():
    body = ('{"nodes":[{"id":"S","type":"source","head":30},'
            '{"id":"A","type":"demand","demand":1},'
            '{"id":"B","type":"demand","demand":1}],'
            '"pipes":[{"id":"P1","start":"S","end":"A","r":1},'
            '{"id":"P2","start":"A","end":"B","r":1}],'
            '"min_head":0,"resupply_nodes":["A",NaN]}')
    resp = client.post("/api/plan-resupply", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]
