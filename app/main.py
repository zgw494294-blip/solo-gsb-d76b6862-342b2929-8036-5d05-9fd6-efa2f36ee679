"""FastAPI 入口：/api/solve 与 /api/analyze-outages 负责校验与求解，其余路径提供原生前端静态页面。"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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


async def _load_payload(request: Request) -> tuple[Any, JSONResponse | None]:
    """读取原始请求体并解析 JSON；任何问题转成统一的 400 响应。"""
    raw = await request.body()
    if not raw:
        return None, JSONResponse(status_code=400, content={
            "ok": False, "error": "请求体为空，请先填写节点与管段数据。"})
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


# 静态资源（index.html / style.css / app.js）——该路由必须最后注册
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
