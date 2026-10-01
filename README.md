# 闭环供水管网平衡台

本地运行的**闭环供水管网稳态平衡**工具：在浏览器中录入节点（1 个定水头水源 + 若干非负需水量的用水节点）与管段（正阻力），后端用 **Python / FastAPI + SciPy** 求解整个管网的稳态水头分布、各管段**带符号流量**与水源总供水量，并按 **1e-6 容差**独立复核节点水量守恒与每根管段的压降定律；前端用**原生 HTML/CSS/JavaScript** 提供可编辑表格、可拖拽的网络图、流向箭头与低水头节点告警。

## 一条命令启动

```bash
docker compose up --build
```

启动后在本机浏览器打开：

```
http://localhost:8000
```

停止：`Ctrl+C`（后台运行用 `docker compose down`）。

> 需要 Docker 20.10+（支持 Compose v2 插件）。端口被占用时修改 `docker-compose.yml` 里的 `"8000:8000"` 左侧端口即可。

## 不用 Docker 的本地运行方式

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

同样访问 http://localhost:8000 。

## 使用说明

1. **节点表**：2～10 个、编号唯一；类型二选一：
   - `水源`：恰好 1 个，填写**固定水头** H_s（有限数值）；
   - `用水`：填写**非负需水量** d。
2. **管段表**：1～16 根、编号唯一；选择**起点/终点**（必须不同，且任意两节点间最多一根管），填写**正阻力 r**。
3. 顶部填写**最低水头限值**，点击 **求解**。
4. 结果区展示：
   - 水源总供水量、总需水量、供耗差；
   - 各节点水头（低于限值的行与图上节点**标红**）；
   - 各管段带符号流量 q、实际流向、`H起−H终` 与压降残差；
   - 网络图上箭头表示**实际流向**（蓝色与录入方向同向，橙色为反向），数字为带符号流量。
5. 点击 **载入示例** 可快速查看一个 5 节点、7 管段的环状管网。
6. **逐根停管分析**：先完成一次稳态求解得到基线，再点击顶栏的 **停管分析**，
   后端对**每根管段各构造一个独立情景**（仅停用该管，其余不变）：
   - **断联情景**：移除后若有用水节点与水源断开，列出**失供节点**与**失供需水量之和**，
     不为该情景生成水头；
   - **连通情景**：仍全部连通则重新求解，列出**最低用水节点水头**、**低于当前最低水头限值的节点**
     以及**比基线新增的低水头节点**；
   - 某个情景单独**不收敛**时单独标明并**排在最后**，不影响其余情景。
   - 排序：断联情景按失供需水量**降序**排在最前，连通情景按最低用水节点水头**升序**随后，
     同分保持管段录入顺序。
   - 只要**编辑节点、管段或最低水头限值**，旧的停管分析立即清除（需要重新点击“停管分析”）。

输入有问题（非有限数值、负需水量、阻力非正、重复编号、端点悬空、图不连通、水源数 ≠ 1 等）或求解器未收敛时，页面会用中文说明具体原因。

## 水力模型

管段 p 按录入方向记 i→j，带符号流量 q_p（正：i→j；负：j→i），采用二次压降定律：

```
H_i − H_j = r_p · q_p · |q_p|
```

每个用水节点 k 满足水量守恒（**净流入 = 需水量**）：

```
Σ_p a_{k,p} · q_p = −d_k
```

其中 a_{k,p} 为节点-管段关联矩阵（起点 +1、终点 −1），故 A·q 为节点净流出。水源节点给定固定水头，因此未知量（n−1 个用水节点水头 + m 个管段流量）与方程数恰好相等，构成平方非线性方程组，用 Newton 型方法求解：

- 以水源为根做 BFS 生成树，按子树需水量摊派初始流量、沿程估算初始水头；
- 依次尝试 `scipy.optimize.root` 的 `hybr`（MINPACK）与 `lm`（Levenberg–Marquardt），并配有带 Armijo 回退的自实现全量 Newton；
- 内部目标残差 1e-9，解出后再以 **1e-6** 容差独立复核每段压降、每个节点守恒以及水源供水量与总需水量之差。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/solve` | 入参 `{nodes, pipes, min_head}`，返回水头、流量、流向、供水量与各项残差；非法输入返回 `422`/`400` 与中文原因 |
| `POST` | `/api/analyze-outages` | 入参与 `/api/solve` 相同；先求基线，再对每根管段各评估一个“单独停用”情景并按风险排序返回 |
| `POST` | `/api/locate-leak` | 入参为管网参数 + `measurements`（2～8 个互异用水节点的实测水头）+ 正的 `leak_upper_bound`；逐用水节点拟合单点漏量并按测点误差排序 |
| `POST` | `/api/plan-resupply` | 入参为管网参数 + `resupply_nodes`（2～6 个互异、表内需水量为正的用水节点）；先求停供基线，再枚举包含空集在内的全部复供组合，返回恢复需水量总量最大的可行组合 |
| `POST` | `/api/isolation-jobs` | 创建停管隔离作业：入参为管网参数 + 可选 `allow_unserved`（允许暂时失供的用水节点数组，可为空）；管段初始全开，初始有用水节点欠压或不收敛则不创建；返回作业 ID 与版本 0 快照 |
| `GET` | `/api/isolation-jobs/{job_id}` | 读取作业当前版本的完整快照（关闭管段、失供节点、连通区稳态结果）；作业不存在返回 `404` |
| `POST` | `/api/isolation-jobs/{job_id}/toggle` | 入参 `{"version", "pipe_id"}`：切换一根现有管段的开闭；规则满足且收敛才提交（关闭集合+版本+1+稳态结果），否则全部保持原状；版本不一致返回 `409` |
| `GET` | `/api/health` | 健康检查 |

请求示例：

```json
{
  "min_head": 0,
  "nodes": [
    {"id": "S", "type": "source", "head": 30.0},
    {"id": "A", "type": "demand", "demand": 2.0}
  ],
  "pipes": [
    {"id": "P1", "start": "S", "end": "A", "r": 1.0}
  ]
}
```

### 逐根停管分析 `POST /api/analyze-outages`

> 停管分析与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

请求体与 `/api/solve` 完全一致（沿用同一套输入校验）。响应中 `analysis.baseline` 为基线稳态解
（结构与 `/api/solve` 的 `result` 相同），`analysis.scenarios` 为每根管段一个、已按风险排序的情景：

- `status: "disconnected"`：含 `unserved_nodes`（失供节点，按节点录入顺序）与
  `unserved_demand`（失供需水量之和），**不返回水头**；
- `status: "connected"`：含 `min_demand_head` / `min_demand_head_node`（最低用水节点水头及节点）、
  `below_min_head`（低于当前限值的节点）、`new_below_min_head`（比基线新增的低水头节点）
  以及 `solution`（该子网上的完整稳态解）；
- `status: "not_converged"`：含 `error`（中文原因），该情景单独排最后，其余情景不受影响。

排序键为：断联（组 0）按 `-unserved_demand`、连通（组 1）按 `min_demand_head` 升序、
不收敛（组 2）最后；组内同分按管段录入顺序。`counts` 给出三类情景的数量。

curl 调用示例：

```bash
curl -s http://localhost:8000/api/analyze-outages \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 0,
    "nodes": [
      {"id": "S", "type": "source", "head": 50.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 3.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0}
    ]
  }'
```

返回（节选）：两根管都是桥，停用 P1 会让 A、B 同时失供（Σd=5），排在停用 P2（仅 B 失供，Σd=3）之前：

```json
{
  "ok": true,
  "analysis": {
    "counts": {"disconnected": 2, "connected": 0, "not_converged": 0},
    "scenarios": [
      {"rank": 1, "pipe_id": "P1", "status": "disconnected",
       "unserved_nodes": ["A", "B"], "unserved_demand": 5.0},
      {"rank": 2, "pipe_id": "P2", "status": "disconnected",
       "unserved_nodes": ["B"], "unserved_demand": 3.0}
    ]
  }
}
```

### 单点漏损定位 `POST /api/locate-leak`

> 漏损定位与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

在管网参数（与 `/api/solve` 完全一致，沿用同一套输入校验）之外，请求体还需包含：

- `measurements`：**2～8 个**实测水头测点，每项为 `{"node": 节点编号, "head": 实测水头}`，
  测点必须**互异且全部为用水节点**，实测水头必须是有限数值；
- `leak_upper_bound`：正的有限数值，即每个候选节点允许增加漏量的搜索上限。

后端先求**无漏损基线**（基线不收敛时整个请求返回 `422` 并说明原因），随后在
**“至多一个用水节点增加漏量”** 的假定下，对每个用水节点各构造一个候选：把该节点需水量
增加 x（x ∈ [0, 上限]），重新做稳态求解，取测点预测水头与实测水头的**均方根误差 RMSE**
作为目标；用黄金分割在 `[0, 上限]` 内一维寻优，**端点 0 与上限都显式参与比较**
（区间内部偶尔求解失败的点按 +∞ 跳过，搜索会自然收缩到可解区域）。

响应 `leak_analysis` 含：

- `baseline_rmse` / `baseline_predicted_heads`：无漏损基线的测点 RMSE 与基线预测水头，
  `baseline` 为完整基线稳态解（结构与 `/api/solve` 的 `result` 相同）；
- `candidates`：每个用水节点一个，按 RMSE **升序**排列，同分按**节点录入顺序**：
  - 成功候选：`status: "fitted"`、`leak`（拟合漏量，可能为端点 0）、`predicted_heads`
    （各测点预测水头）、`head_errors`（预测−实测，带符号）、`rmse`、`sse`、`rank`；
  - 失败候选：`status: "not_converged"`、`error`（中文原因），**单独标明并排最后**，
    不影响其他候选（`counts` 给出两类数量）；
- `indistinguishable` / `indistinguishable_nodes`：若至少有两个候选与最优 RMSE 相差
  **不超过 1e-4**，这些候选一律标为**无法区分**；
- `leak_detected` / `leak_node` / `conclusion`：**最优漏量为 0 时不判定存在漏损**
  （`leak_detected=false`、`leak_node=null`）；否则给出最可能的漏损节点与拟合漏量，
  并在结论文字中说明是否存在无法区分的并列候选。

curl 调用示例（该管网真值为 B 点额外漏损 0.8，实测水头取 A、C 两点）：

```bash
curl -s http://localhost:8000/api/locate-leak \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 10,
    "nodes": [
      {"id": "S", "type": "source", "head": 30.0},
      {"id": "A", "type": "demand", "demand": 1.0},
      {"id": "B", "type": "demand", "demand": 1.0},
      {"id": "C", "type": "demand", "demand": 1.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0},
      {"id": "P3", "start": "B", "end": "C", "r": 1.0},
      {"id": "P4", "start": "C", "end": "S", "r": 1.0},
      {"id": "P5", "start": "S", "end": "B", "r": 2.5}
    ],
    "measurements": [
      {"node": "A", "head": 27.959627},
      {"node": "C", "head": 27.959627}
    ],
    "leak_upper_bound": 2.0
  }'
```

返回（节选）：无漏损基线 RMSE≈0.7462；B 点以漏量 0.8 把 RMSE 降到 ≈0，明显优于 A、C
（RMSE≈0.1151），故判定漏损在 B；`head_errors` 为预测水头减实测水头：

```json
{
  "ok": true,
  "leak_analysis": {
    "leak_upper_bound": 2.0,
    "tie_tolerance": 0.0001,
    "baseline_rmse": 0.746177,
    "baseline_predicted_heads": {"A": 28.705804, "C": 28.705804},
    "candidates": [
      {"rank": 1, "node_id": "B", "status": "fitted", "leak": 0.8,
       "predicted_heads": {"A": 27.959627, "C": 27.959627},
       "head_errors": {"A": 0.0, "C": 0.0}, "rmse": 0.0},
      {"rank": 2, "node_id": "A", "status": "fitted", "leak": 0.74109,
       "predicted_heads": {"A": 27.874847, "C": 28.098663},
       "head_errors": {"A": -0.08478, "C": 0.139036}, "rmse": 0.115149},
      {"rank": 3, "node_id": "C", "status": "fitted", "leak": 0.74109,
       "predicted_heads": {"A": 28.098663, "C": 27.874847},
       "head_errors": {"A": 0.139036, "C": -0.08478}, "rmse": 0.115149}
    ],
    "counts": {"fitted": 3, "not_converged": 0},
    "best": {"node_id": "B", "leak": 0.8, "rmse": 0.0},
    "indistinguishable": false,
    "indistinguishable_nodes": ["B"],
    "leak_detected": true,
    "leak_node": "B",
    "conclusion": "最可能的单点漏损位于用水节点“B”，拟合漏量为 0.8（测点 RMSE=1.07e-14）。"
  }
}
```

非法输入（测点数不在 2～8、测点重复或不是用水节点、实测水头/上限非有限值、上限非正、
原有管网校验未通过等）返回 `422`/`400` 并附中文原因。

### 停供节点同时复供组合优选 `POST /api/plan-resupply`

> 复供优选与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

在管网参数（与 `/api/solve` 完全一致，沿用同一套输入校验）之外，请求体还需包含：

- `resupply_nodes`：**2～6 个互异**的候选停供节点编号，必须**全部为表内需水量为正**
  的用水节点（不能是水源，也不能是零需水节点）；候选的处理顺序一律按**节点录入顺序**，
  与数组书写顺序无关。

后端把这些候选节点视为**当前停供**：

1. **先求复供前基线（空集）**：所有候选节点按**零需水**计算，其他节点（含非候选用水
   节点）保持表内需水量不变；基线若有用水节点水头低于当前最低水头限值，则现有管网在
   复供前即**欠压**，`status: "baseline_below_limit"`，**无法复供**（不再评估其他组合）。
2. 基线合格时，再核对包含空集在内的 **2^k 个组合**：每个组合中被选中的候选**恢复表内
   需水量**，未选中的候选继续按零需水；每个组合都独立做稳态求解。只有**稳态收敛且所有
   用水节点水头均不低于最低水头限值**的组合才标记为 `feasible`（可选）。
3. 在所有可行组合中，选**恢复需水量总和最大**者；**总量相同先选节点更多的，再按节点
   录入顺序的字典序选**（例：总量同为 4 时 `{A,B}` 胜 `{C}`，`{A,B}` 胜 `{A,C}`）。
4. **求解失败绝不按欠压处理**：基线不收敛时 `status: "baseline_not_converged"`、
   `baseline` 为 `null`，无法确认最优；任一非空组合不收敛时
   `status: "not_confirmed"`，该组合记为 `not_converged`（不计入欠压数）并排最后，
   其余组合照常评估，响应同时给出当前已确认可行组合中的**临时最优**
   （`selected_provisional: true`），并在 `conclusion` 中说明该结果可能被未求解的
   组合超越。

响应 `resupply_analysis` 含：

- `status`：`optimal`（全部组合收敛且最优已确认）/ `baseline_below_limit`（基线欠压，
  无法复供）/ `baseline_not_converged`（基线不收敛）/ `not_confirmed`（部分组合不收敛，
  无法确认最优）之一；`optimal_confirmed` 仅在 `optimal` 时为 `true`；
- `baseline`：复供前基线的完整稳态解（结构与 `/api/solve` 的 `result` 相同；
  基线不收敛时为 `null`，原因在 `baseline_error`），`baseline_below_min_head` 为基线
  低于限值的用水节点；
- `combinations`：全部 2^k 个组合（空集排第一参与枚举），按“可行→欠压→不收敛”分组、
  组内按上述优选规则排序并带 `rank`：
  - `feasible`：`nodes`（复供节点，按节点录入顺序）、`node_count`、`restored_demand`
    （恢复需水量总和）、`min_demand_head` / `min_demand_head_node`、
    `below_min_head: []` 与该组合的完整 `solution`；
  - `below_limit`：稳态收敛但有用水节点低于限值，含 `below_min_head`，**不可选**；
  - `not_converged`：含 `error`（中文原因），单独排最后，**不计为欠压**；
- `selected`：最优（或临时最优）组合记录；`restored_demand_total` 为恢复总量；
  `unrestored_nodes` 为仍按零需水、**未复供**的候选节点；
- `table_demands` / `table_demand_total`：候选节点的表内需水量与总和；
  `counts` 给出三类组合数量；`conclusion` 为中文结论。

curl 调用示例（树状网 S–A–B–C，候选 A/B/C 表内需水均为 2，限值 9）：

```bash
curl -s http://localhost:8000/api/plan-resupply \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 9,
    "nodes": [
      {"id": "S", "type": "source", "head": 30.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 2.0},
      {"id": "C", "type": "demand", "demand": 2.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0},
      {"id": "P3", "start": "B", "end": "C", "r": 1.0}
    ],
    "resupply_nodes": ["A", "B", "C"]
  }'
```

返回（节选）：复供前基线 A/B/C 全停（按零需水），各点水头均为 30；三节点同时复供会让
末端水头跌到 −26，`{A,C}`、`{B,C}` 也分别因 C、B/C 欠压不可选；可行组合中
`{A,B}` 恢复总量 4 最大（此时 H_A=14、H_B=H_C=10 均不低于限值 9），C 未复供：

```json
{
  "ok": true,
  "resupply_analysis": {
    "status": "optimal",
    "optimal_confirmed": true,
    "min_head": 9.0,
    "resupply_nodes": ["A", "B", "C"],
    "table_demands": {"A": 2.0, "B": 2.0, "C": 2.0},
    "table_demand_total": 6.0,
    "evaluated_combinations": 8,
    "total_combinations": 8,
    "baseline_below_min_head": [],
    "counts": {"feasible": 5, "below_limit": 3, "not_converged": 0},
    "selected": {
      "rank": 1,
      "status": "feasible",
      "nodes": ["A", "B"],
      "node_count": 2,
      "restored_demand": 4.0,
      "min_demand_head": 10.0,
      "min_demand_head_node": "B",
      "below_min_head": []
    },
    "restored_demand_total": 4.0,
    "unrestored_nodes": ["C"],
    "failed_combinations": [],
    "conclusion": "全部 8 个组合均已稳态收敛；在所有用水节点水头均不低于最低水头限值 9 的可行组合中，恢复需水量总和最大的组合为 {A、B}，恢复总量 4。"
  }
}
```

全部 8 个组合（节选，`feasible` 在前、`below_limit` 在后）：

| rank | 组合 | 状态 | 恢复总量 | 最低用水节点水头 | 低于限值节点 |
| --- | --- | --- | --- | --- | --- |
| 1 | `{A,B}` | feasible | 4 | 10（B） | — |
| 2 | `{A}` | feasible | 2 | 26（A） | — |
| 3 | `{B}` | feasible | 2 | 22（B） | — |
| 4 | `{C}` | feasible | 2 | 18（C） | — |
| 5 | `{}`（基线/空集） | feasible | 0 | 30 | — |
| 6 | `{A,B,C}` | below_limit | 6 | −26（C） | A、B、C |
| 7 | `{A,C}` | below_limit | 4 | 6（C） | C |
| 8 | `{B,C}` | below_limit | 4 | −6（C） | B、C |

非法输入（候选数不在 2～6、候选重复、不是用水节点或为水源、表内需水量非正、
`resupply_nodes` 不是数组、原有管网校验未通过等）返回 `422`/`400` 并附中文原因；
基线欠压与“无法确认最优”属于业务结论，HTTP 状态码仍为 `200`，请依据 `status` 字段区分。

### 停管隔离作业 `POST /api/isolation-jobs`（分步停管/复管）

> 停管隔离作业与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。作业**只保存在单进程内存中，服务重启
> 即全部清空**；该功能仅提供 HTTP API，**没有对应页面**。

一次“停管隔离作业”表示对同一张现有管网进行**一系列逐步的停管/复管操作**：

1. **创建作业** `POST /api/isolation-jobs`：请求体在 `/api/solve` 参数之外可带
   `allow_unserved`——**允许暂时失供的现有用水节点编号数组（可为空数组或省略）**。
   管段初始全部开启；后端沿用同一套校验与稳态求解，**初始状态下只要有用水节点欠压
   （水头低于 `min_head`）或求解不收敛，就不创建作业**（返回 `422` 与中文原因）。
   创建成功返回作业 `job_id` 与初始 `version: 0` 的完整状态快照。
2. **读取状态** `GET /api/isolation-jobs/{job_id}`：返回该作业当前版本的完整快照，
   快照整体取自同一版本，**绝不混合不同版本的数据**；作业不存在返回 `404`。
3. **切换一根管段** `POST /api/isolation-jobs/{job_id}/toggle`：请求体
   `{"version": 当前版本号, "pipe_id": "现有管段编号"}`，把该管段**由开转关或由关转开**。
   后端按**全部关闭管段**重新计算水源连通区并校验：
   - **失供节点**（与水源断开的用水节点）必须全部在 `allow_unserved` 集合内；
   - 连通区内用水节点**按原需水量**重新稳态求解，且**任何用水节点不得欠压**；
   - 失供节点**保留原需水量**，重新连通后自动恢复参与求解；
   - 连通区**仅剩水源时供水量为零**（不再调用求解器）。

   全部满足才**一次性提交**：关闭集合更新、`version` 加一、稳态结果替换；非法操作
   （未知管段、版本号类型错误等）、求解不收敛或上述规则不满足时，**关闭集合、版本号、
   稳态结果全部保持原状**，返回 `422` 并说明原因。`version` 与当前版本不一致时返回
   **`409`**——**同一版本的并发切换最多成功一次**，先到者成功，其余一律 `409`，
   请重新读取最新状态后再试。

每次成功响应都返回最新快照，主要字段：`closed_pipes` / `open_pipes` / `pipe_states`
（关闭管段与逐管开闭）、`unserved_nodes` / `unserved_demand`（失供节点与失供量之和）、
`component_nodes` / `component_pipes`（水源连通区）、`source_only`、`source_supply` /
`total_demand`，以及 `solution`（连通区上的完整稳态解，结构与 `/api/solve` 的
`result` 相同；仅剩水源时为供水量为零的平凡解）。

#### 操作示例

以树状网 `S(50) ─P1→ A(需2) ─P2→ B(需3)` 为例，允许 B 暂时失供：

```bash
# 1) 创建作业（管段初始全开），记下返回的 job_id
curl -s -X POST http://localhost:8000/api/isolation-jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 0,
    "nodes": [
      {"id": "S", "type": "source", "head": 50.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 3.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0}
    ],
    "allow_unserved": ["B"]
  }'
```

返回（节选）：初始版本 0，无关闭管段、无失供节点，供水量 5：

```json
{"ok": true, "job": {"job_id": "05ced70d…（每次创建随机生成）", "version": 0,
 "closed_pipes": [], "unserved_nodes": [], "component_nodes": ["S", "A", "B"],
 "source_supply": 5.0, "total_demand": 5.0}}
```

```bash
# 2) 携带当前版本关闭 P2：B 失供但在允许集合内，操作成功，版本 0 → 1
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipe_id": "P2"}'
```

返回（节选）：连通区只剩 S、A，按原需水量求解，供水量降为 2：

```json
{"ok": true, "toggled_pipe": "P2", "toggled_to": "closed",
 "job": {"version": 1, "closed_pipes": ["P2"], "unserved_nodes": ["B"],
 "unserved_demand": 3.0, "component_nodes": ["S", "A"], "source_supply": 2.0}}
```

#### 冲突示例（同一版本最多成功一次）

```bash
# 3) 仍携带旧版本 0 再操作：版本已推进到 1，返回 409，状态不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipe_id": "P1"}'
```

```json
{"ok": false, "current_version": 1,
 "error": "版本冲突：请求携带的版本为 0，作业当前版本已为 1；同一版本的切换最多成功一次，请重新读取最新状态后再试。"}
```

并发场景同理：多个客户端携带**同一版本**同时切换时，**最多一个成功**（版本 +1），
其余全部收到 `409`，重新读取后再携带新版本重试。

#### 规则不满足示例（全部保持原状）

```bash
# 4) 携带当前版本 1 关闭 P1：A 将失供但不在允许集合内，返回 422，状态不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "pipe_id": "P1"}'
```

```json
{"ok": false,
 "error": "切换后用水节点 “A” 将失供，但其不在允许暂时失供的节点集合内；本次操作未生效，关闭集合、版本与稳态结果全部保持原状。"}
```

欠压（切换后连通区内有用水节点水头低于 `min_head`）与求解不收敛同样返回 `422`
并全部保持原状；**失败不消耗版本号**，用同一版本改发合法操作仍可成功。

```bash
# 5) 重开 P2：B 恢复原需水量重新参与求解，供水量回到 5，版本 1 → 2
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "pipe_id": "P2"}'

# 6) 随时读取当前版本完整快照；服务重启后作业清空，再读返回 404
curl -s http://localhost:8000/api/isolation-jobs/<job_id>
```

## 输入约束汇总

- 节点 2～10 个，编号为非空且唯一的字符串；
- 恰好 1 个 `source`（固定水头，有限数值），其余为 `demand`（需水量为非负有限数）；
- 管段 1～16 根，编号唯一，起点/终点均为已定义且互不相同的节点，同一对节点之间只允许一根管段，阻力 r 为正有限数；
- 管网必须是**无向连通图**（孤立节点/孤岛一律拒绝）；
- 不接受 `NaN` / `Infinity`（含 JSON 文本中的 `NaN`/`Infinity` 记号）；
- 仅 `/api/locate-leak` 额外要求：`measurements` 为 2～8 个测点，节点编号互异且均为
  用水节点，实测水头为有限数值；`leak_upper_bound` 为正有限数。
- 仅 `/api/plan-resupply` 额外要求：`resupply_nodes` 为 2～6 个互异节点编号，均为
  表内需水量为正的用水节点（不能是水源或零需水节点）；候选顺序按节点录入顺序归并。
- 仅停管隔离作业：创建时 `allow_unserved` 为可选数组（可为空），元素为**互异的现有
  用水节点**编号；切换时请求体为 `{"version": 非负整数, "pipe_id": 现有管段编号}`，
  `version` 必须等于作业当前版本（同一版本最多成功切换一次，冲突返回 `409`）；
  作业只存于单进程内存，重启即清空。

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口：/api/solve、/api/analyze-outages、/api/locate-leak、/api/plan-resupply、/api/isolation-jobs（创建/读取/切换）、/api/health、静态页面
│   ├── solver.py          # 输入校验 + Newton 稳态求解 + 1e-6 复核 + 逐根停管情景分析 + 单点漏损定位（黄金分割）+ 停供节点复供组合枚举
│   ├── isolation.py       # 停管隔离作业：内存作业注册表、水源连通区计算、失供/欠压规则、版本乐观锁（并发同一版本最多成功一次）
│   └── static/
│       ├── index.html     # 原生页面（含停管分析入口与结果表）
│       ├── style.css
│       └── app.js         # 可编辑表格、力图布局、拖拽、流向绘制、停管分析
├── tests/
│   ├── test_solver.py     # pytest：解析解、环状网、校验分支、API、随机压力测试、停管分析排序、漏损定位、复供组合优选
│   └── test_isolation.py  # pytest：停管隔离作业的创建/切换/规则/版本冲突/并发/快照一致性
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q
```

测试包含单管解析解、反向录入符号、环状网独立复核、低水头标记、各类非法输入、`NaN` JSON 拒绝，以及 60 个随机连通管网（2～10 节点、最多 16 管、阻力跨 6 个数量级）的收敛性与残差检查；停管分析部分覆盖树状网全断联按失供量排序、环+枝混合分组排序、比基线新增低水头节点、同分保持录入顺序、单情景不收敛单独排最后，以及 `/api/analyze-outages` 的 200/422/400 分支；漏损定位部分覆盖真值漏点与漏量回收、无漏损基线 RMSE=0 不判漏、对称网络多候选无法区分、最优漏量落在上限端点、测点/上限各类非法输入、基线不收敛整体报错、单候选失败隔离排最后，以及 `/api/locate-leak` 的 200/422/400 分支；复供优选部分覆盖可行组合按恢复总量排序、总量同分先比节点数再按录入顺序字典序、空集最优、基线欠压无法复供、非候选节点需水不变、基线不收敛与单组合不收敛的“无法确认最优”区分（求解失败不计欠压）、候选 2～6 个/互异/正需水等校验分支，以及 `/api/plan-resupply` 的 200/422/400 分支；停管隔离作业部分覆盖创建（初始欠压/不收敛不创建、`allow_unserved` 校验与录入顺序归并）、切换管段的失供/欠压/不收敛规则（失败全部保持原状且不消耗版本）、失供节点保留原需水量并在重连后恢复、仅剩水源供水量为零、版本冲突 409、多线程并发同一版本最多成功一次、读取快照不混合版本、两作业相互独立，以及未知作业 404 与各类 400/422 分支。
