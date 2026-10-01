"""FastAPI 入口：/api/solve 等求解接口、/api/isolation-jobs 停管隔离作业接口，其余路径提供原生前端静态页面。"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .isolation import (
    IsolationRuleError,
    JobNotFoundError,
    VersionConflictError,
    create_job,
    get_snapshot,
    toggle_pipe,
)
from .solver import (
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="闭环供水管网平衡台", version="1.0.0")


def _reject_constant(value: str) -> Any:
    """拒绝 JSON 标准之外的 NaN / Infinity / -Infinity 记号。"""
    raise ValueError(f"非法的 JSON 数值记号：{value}")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _load_payload(request: Request, *, empty_hint: str | None = None
                        ) -> tuple[Any, JSONResponse | None]:
    """读取原始请求体并解析 JSON；任何问题转成统一的 400 响应。"""
    raw = await request.body()
    if not raw:
        return None, JSONResponse(status_code=400, content={
            "ok": False,
            "error": empty_hint or "请求体为空，请先填写节点与管段数据。"})
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        return None, JSONResponse(status_code=400, content={
            "ok": False,
            "error": f"请求体不是合法 JSON：{exc}。请确认所有输入均为有限数值。"})
    return payload, None


@app.post("/api/solve")
async def solve(request: Request) -> JSONResponse:
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        network = validate_payload(payload)
        result = solve_network(network)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "result": result})


@app.post("/api/analyze-outages")
async def analyze_outages(request: Request) -> JSONResponse:
    """逐根停管情景分析：先求基线，再对每根管段各评估一个移除情景。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        network = validate_payload(payload)
        analysis = analyze_pipe_outages(network)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "analysis": analysis})


@app.post("/api/locate-leak")
async def locate_leak(request: Request) -> JSONResponse:
    """基于实测水头的单点漏损定位：沿用管网校验与稳态求解，逐用水节点拟合漏量。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        case = validate_leak_payload(payload)
        analysis = locate_single_leak(case)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "leak_analysis": analysis})


@app.post("/api/plan-resupply")
async def plan_resupply_endpoint(request: Request) -> JSONResponse:
    """停供节点同时复供组合优选：先求复供前基线，再核对包含空集在内的全部组合。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        case = validate_resupply_payload(payload)
        analysis = plan_resupply(case)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "resupply_analysis": analysis})


# --------------------------------------------------------------------------- #
# 停管隔离作业（仅存于单进程内存，重启即清空；无对应页面）
# --------------------------------------------------------------------------- #
_ISOLATION_ERRORS = (
    ValidationError,
    IsolationRuleError,
    SolverNotConverged,
    VersionConflictError,
    JobNotFoundError,
)


def _isolation_error(exc: Exception) -> JSONResponse:
    """把停管隔离作业的各类失败映射为统一的中文错误响应（作业状态保持原状）。"""
    if isinstance(exc, JobNotFoundError):
        return JSONResponse(status_code=404, content={
            "ok": False,
            "error": f"停管隔离作业“{exc.args[0]}”不存在"
                     "（作业仅存于单进程内存，服务重启后即清空）。"})
    if isinstance(exc, VersionConflictError):
        return JSONResponse(status_code=409, content={
            "ok": False, "error": str(exc), "current_version": exc.current})
    return JSONResponse(status_code=422,
                        content={"ok": False, "error": str(exc)})


@app.post("/api/isolation-jobs")
async def create_isolation_job(request: Request) -> JSONResponse:
    """创建停管隔离作业：沿用校验与稳态求解，初始欠压或不收敛则不创建。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad
    try:
        snapshot = create_job(payload)
    except _ISOLATION_ERRORS as exc:
        return _isolation_error(exc)
    return JSONResponse(content={"ok": True, "job": snapshot})


@app.get("/api/isolation-jobs/{job_id}")
async def read_isolation_job(job_id: str) -> JSONResponse:
    """读取作业当前版本的完整快照；快照整体来自同一版本，绝不混合不同版本的数据。"""
    try:
        snapshot = get_snapshot(job_id)
    except JobNotFoundError as exc:
        return _isolation_error(exc)
    return JSONResponse(content={"ok": True, "job": snapshot})


@app.post("/api/isolation-jobs/{job_id}/toggle")
async def toggle_isolation_pipe(job_id: str, request: Request) -> JSONResponse:
    """携带当前版本切换一根现有管段的开闭；成功才提交新版本，失败全部保持原状。"""
    payload, bad = await _load_payload(
        request, empty_hint="请求体为空，请提供 version（作业当前版本号）与 pipe_id。")
    if bad is not None:
        return bad
    try:
        snapshot = toggle_pipe(job_id, payload)
    except _ISOLATION_ERRORS as exc:
        return _isolation_error(exc)
    pipe_id = payload["pipe_id"].strip()
    return JSONResponse(content={
        "ok": True,
        "toggled_pipe": pipe_id,
        "toggled_to": "closed" if pipe_id in snapshot["closed_pipes"] else "open",
        "job": snapshot,
    })


# 静态资源（index.html / style.css / app.js）——该路由必须最后注册
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
