"""管理接口：场景校验 / 版本化发布 / 查询 / 版本列表 / 激活 / 删除。"""
from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..db import get_conn
from ..scenario import ScenarioError, parse_scenario
from ..store import store

router = APIRouter(prefix="/__scenarios", tags=["scenarios"])


async def _read_definition(request: Request) -> str:
    raw = (await request.body()).decode("utf-8")
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        import json
        data = json.loads(raw)
        if isinstance(data, dict) and "definition" in data:
            if not isinstance(data["definition"], str):
                return yaml.safe_dump(data["definition"], allow_unicode=True, sort_keys=False)
            return data["definition"]
        # 整个 JSON 就当作场景定义
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return raw


def _summary(scenario) -> dict[str, Any]:
    return {
        "name": scenario.name,
        "description": scenario.description,
        "initial_state": scenario.initial_state,
        "states": scenario.states,
        "routes": [
            {
                "id": r.id,
                "path": r.path,
                "methods": r.methods,
                "rules": [
                    {"name": rule.name, "priority": rule.priority,
                     "next_state": rule.next_state}
                    for rule in r.rules
                ],
            }
            for r in scenario.routes
        ],
    }


@router.post("/validate")
async def validate_scenario(request: Request) -> JSONResponse:
    """仅校验不入库。"""
    text = await _read_definition(request)
    try:
        scenario = parse_scenario(text)
    except ScenarioError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
    except yaml.YAMLError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": f"YAML 解析失败: {exc}"})
    return JSONResponse({"valid": True, "scenario": _summary(scenario)})


@router.get("")
async def list_scenarios() -> JSONResponse:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, current_revision, created_at, updated_at"
        " FROM scenarios ORDER BY name"
    ).fetchall()
    return JSONResponse({
        "scenarios": [
            {"name": r["name"], "current_revision": r["current_revision"],
             "created_at": r["created_at"], "updated_at": r["updated_at"],
             **_summary(store.get(r["name"]))}
            for r in rows
        ]
    })


@router.get("/{name}")
async def get_scenario(name: str) -> JSONResponse:
    conn = get_conn()
    row = conn.execute(
        "SELECT name, current_revision, created_at, updated_at"
        " FROM scenarios WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "scenario_not_found", "name": name})
    return JSONResponse({
        "name": row["name"],
        "current_revision": row["current_revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "scenario": _summary(store.get(name)),
    })


@router.get("/{name}/yaml", response_class=PlainTextResponse)
async def get_scenario_yaml(name: str, revision: int | None = None) -> PlainTextResponse:
    """取回原始 YAML；默认当前版本，可用 ?revision=N 取指定历史版本。"""
    conn = get_conn()
    if revision is None:
        row = conn.execute(
            """
            SELECT r.definition FROM scenarios s
            JOIN scenario_revisions r
              ON r.name = s.name AND r.revision = s.current_revision
            WHERE s.name = ?
            """,
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT definition FROM scenario_revisions WHERE name = ? AND revision = ?",
            (name, revision),
        ).fetchone()
    if row is None:
        return PlainTextResponse(
            f'{{"error":"scenario_not_found","name":"{name}"}}',
            status_code=404,
            media_type="application/json",
        )
    return PlainTextResponse(row["definition"], media_type="application/yaml")


@router.get("/{name}/revisions")
async def list_revisions(name: str) -> JSONResponse:
    """版本列表：全部不可变 revision 及当前指针。"""
    conn = get_conn()
    scenario = conn.execute(
        "SELECT current_revision FROM scenarios WHERE name = ?", (name,)
    ).fetchone()
    if scenario is None:
        return JSONResponse(status_code=404, content={"error": "scenario_not_found", "name": name})
    rows = conn.execute(
        "SELECT revision, created_at FROM scenario_revisions"
        " WHERE name = ? ORDER BY revision",
        (name,),
    ).fetchall()
    current = scenario["current_revision"]
    return JSONResponse({
        "name": name,
        "current_revision": current,
        "revisions": [
            {"revision": r["revision"], "created_at": r["created_at"],
             "is_current": r["revision"] == current}
            for r in rows
        ],
    })


@router.post("/{name}/revisions/{revision}/activate")
async def activate_revision(name: str, revision: int) -> JSONResponse:
    """把指定 revision 设为当前版本；只影响此后新建会话的绑定，既有会话不变。"""
    if not store.activate(name, revision):
        return JSONResponse(status_code=404, content={
            "error": "revision_not_found", "name": name, "revision": revision,
        })
    return JSONResponse({
        "activated": True,
        "name": name,
        "current_revision": revision,
        "detail": "仅切换新会话的默认版本；既有会话仍按各自绑定的 revision 执行",
    })


@router.put("/{name}")
async def upsert_scenario(name: str, request: Request) -> JSONResponse:
    """校验并发布新版本（不可变）；内容与当前版本相同时幂等返回原 revision。"""
    text = await _read_definition(request)
    try:
        scenario, revision, created = store.publish(name, text)
    except ScenarioError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
    except yaml.YAMLError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": f"YAML 解析失败: {exc}"})
    if not created:
        action = "unchanged"
    elif revision == 1:
        action = "created"
    else:
        action = "published"
    return JSONResponse({
        "loaded": True,
        "action": action,
        "revision": revision,
        "current_revision": revision,
        "scenario": _summary(scenario),
    })


@router.delete("/{name}")
async def delete_scenario(name: str, request: Request) -> JSONResponse:
    force = request.query_params.get("force") in {"1", "true", "yes"}
    conn = get_conn()
    sessions = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE scenario = ?", (name,)
    ).fetchone()["c"]
    if sessions and not force:
        return JSONResponse(status_code=409, content={
            "error": "scenario_in_use",
            "sessions": sessions,
            "detail": "仍有会话引用该场景；确认级联删除请加 ?force=true",
        })
    if not store.get(name):
        return JSONResponse(status_code=404, content={"error": "scenario_not_found", "name": name})
    if force:
        conn.execute("DELETE FROM events WHERE scenario = ?", (name,))
        conn.execute("DELETE FROM sessions WHERE scenario = ?", (name,))
        conn.commit()
    store.delete(name)
    return JSONResponse({"deleted": True, "name": name})
