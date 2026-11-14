"""FastAPI 路由（薄）：页面渲染 + 少量 JSON API，核心规则全部在纯函数模块。

受控发布流程：预演（POST /api/releases/preview，同指纹幂等）→
阻断全部消除 → 发布（POST /api/releases/{id}/publish，原子切换当前版）。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import control, release as rel, report, schemaio, settings, store
from .schemaio import SchemaError
from .types import DECIMAL_RULES, matrix_table

app = FastAPI(title=settings.APP_NAME)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(settings.BASE_DIR / "app" / "templates"))


def _ctx(request: Request, active: str, **extra) -> dict:
    ctx = {"app_name": settings.APP_NAME, "active": active, "matrix_version": settings.MATRIX_VERSION}
    ctx.update(extra)
    return ctx


def _view(release: dict) -> dict:
    """release + 派生状态（按读取时刻的 current 指针与规则摘要计算）。"""
    return rel.release_view(release, store.load_current(), rel.current_rules_digest())


def _release_or_404(release_id: str):
    try:
        return store.load_release(release_id)
    except KeyError:
        return None


def _index_stats(sample_count: int, releases: list[dict], current: dict) -> dict:
    """总览页只读统计：全部从既有发布记录派生，不改动任何业务判定。"""
    steps = [int(r.get("result", {}).get("summary", {}).get("sql_steps") or 0) for r in releases]
    summaries = [r.get("result", {}).get("summary", {}) for r in releases]
    return {
        "sample_count": sample_count,
        "release_count": len(releases),
        "published_count": len(current),
        "full_count": sum(1 for s in summaries if s.get("level") == "full"),
        "backfill_count": sum(1 for s in summaries if int(s.get("backfill_count") or 0) > 0),
        "breaking_count": sum(1 for s in summaries if not s.get("can_execute", True)),
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0.0,
    }


def _resolve_inputs(payload: dict) -> tuple[dict, dict, dict, str]:
    if not isinstance(payload, dict):
        raise SchemaError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key in settings.SAMPLE_KEYS:
        sample = schemaio.load_sample(sample_key)
        return sample["old"], sample["new"], sample["consumers"], f"样例 {sample_key}"
    old = schemaio.parse_schema(payload.get("old"), "旧 schema")
    new = schemaio.parse_schema(payload.get("new"), "新 schema")
    consumers = schemaio.parse_consumers(payload.get("consumers"), "数据文件清单")
    return old, new, consumers, "自定义输入"


def _check_consistent(old: dict, new: dict, consumers: dict) -> None:
    if old["table"] != new["table"]:
        raise SchemaError(f"新旧 schema 的表名不一致：{old['table']} vs {new['table']}")
    if consumers.get("table") and consumers["table"] != new["table"]:
        raise SchemaError(f"数据文件清单的表名 {consumers['table']} 与新 schema {new['table']} 不一致")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "schemaevo", "matrix_version": settings.MATRIX_VERSION}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    samples = schemaio.load_sample_index()
    current = store.load_current()
    releases = store.list_releases(limit=50)
    views = [_view(r) for r in releases]
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(
            request, "/",
            page="index",
            samples=samples,
            recent=views[:3],
            stats=_index_stats(len(samples), releases, current),
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
async def workbench(request: Request):
    key = (request.query_params.get("sample") or "A").strip().upper()
    if key not in settings.SAMPLE_KEYS:
        key = "A"
    sample = schemaio.load_sample(key)
    # 右侧面板「即时预演」：复用既有纯函数算一遍所选样例，不落库、不产生版本。
    preview = report.build_run(sample["old"], sample["new"], sample["consumers"], f"样例 {key}")
    return templates.TemplateResponse(
        request, "workbench.html",
        _ctx(
            request, "/workbench",
            page="workbench",
            samples=schemaio.load_sample_index(),
            selected=key,
            sample=sample,
            preview=preview,
            old_text=sample["raw_old"],
            new_text=sample["raw_new"],
            consumers_text=sample["raw_consumers"],
        ),
    )


@app.post("/api/releases/preview")
async def api_preview(request: Request):
    """预演：冻结规则 + 两版字段 + 读取方清单，生成（或复用）一个 release。

    相同冻结输入重试返回同一 release（created=false），不新增版本。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        old, new, consumers, source = _resolve_inputs(payload)
        _check_consistent(old, new, consumers)
        release, created = control.preview_release(old, new, consumers, source)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    view = _view(release)
    return {
        "release_id": view["release_id"],
        "version": view["version"],
        "state": view["state"],
        "state_label": view["state_label"],
        "fingerprint": view["fingerprint"],
        "created": created,
        "summary": view["result"]["summary"],
        "redirect": f"/result/{view['release_id']}",
    }


@app.post("/api/releases/{release_id}/publish")
async def api_publish(release_id: str):
    """发布：阻断全部消除后，把当前版指针一次性切到该 release；失败保留上一版。"""
    try:
        release = control.publish_release(release_id)
    except KeyError:
        return JSONResponse({"detail": f"发布记录 {release_id} 不存在"}, status_code=404)
    except rel.ReleaseError as exc:
        body = {"detail": exc.detail, **exc.extra}
        return JSONResponse(body, status_code=exc.status_code)
    view = _view(release)
    return {
        "release_id": view["release_id"],
        "version": view["version"],
        "state": view["state"],
        "state_label": view["state_label"],
        "published_at": view.get("published_at"),
        "redirect": f"/result/{view['release_id']}",
    }


@app.get("/api/releases")
async def api_releases() -> dict:
    views = [_view(r) for r in store.list_releases(limit=50)]
    rows = [{
        "release_id": v["release_id"],
        "table": v["table"],
        "version": v["version"],
        "state": v["state"],
        "state_label": v["state_label"],
        "is_current": v["is_current"],
        "created_at": v["created_at"],
        "published_at": v.get("published_at"),
        "source": v["source"],
        "fingerprint": v["fingerprint"],
        "summary": v["result"]["summary"],
    } for v in views]
    return {"count": len(rows), "releases": rows}


@app.get("/api/releases/{release_id}")
async def api_release(release_id: str):
    release = _release_or_404(release_id)
    if release is None:
        return JSONResponse({"detail": f"发布记录 {release_id} 不存在"}, status_code=404)
    return _view(release)


@app.get("/api/releases/{release_id}/migration.sql", response_class=PlainTextResponse)
async def api_sql(release_id: str):
    """下载迁移脚本：只有「当前生效版」可作为可执行方案下载；其余状态一律 409。"""
    release = _release_or_404(release_id)
    if release is None:
        return PlainTextResponse(f"-- 发布记录 {release_id} 不存在\n", status_code=404)
    view = _view(release)
    if not view["downloadable"]:
        current_id = view.get("current_release_id")
        hint = f"；当前生效版：/api/releases/{current_id}/migration.sql" if current_id else "；当前无生效版"
        return PlainTextResponse(
            f"-- 该版本（{release_id} · v{view['version']}）{view['state_label']}，脚本不可作为可执行方案{hint}\n",
            status_code=409,
        )
    return PlainTextResponse(
        view["result"]["migration"]["sql_text"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report.sql_download_name(view)}"'},
    )


@app.get("/api/samples/{key}")
async def api_sample(key: str):
    try:
        sample = schemaio.load_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "description": sample["description"],
        "expected": sample["expected"],
        "old": sample["raw_old"],
        "new": sample["raw_new"],
        "consumers": sample["raw_consumers"],
    }


@app.get("/api/matrix")
async def api_matrix() -> dict:
    return {"version": settings.MATRIX_VERSION, "promotions": matrix_table(), "decimal_rules": DECIMAL_RULES}


@app.get("/result/{release_id}", response_class=HTMLResponse)
async def result_page(request: Request, release_id: str):
    release = _release_or_404(release_id)
    if release is None:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(
                request, "",
                page="not_found",
                release_id=release_id,
                max_releases=settings.MAX_RELEASES_KEPT,
                error=f"发布记录 {release_id} 不存在（历史记录只保留最近 {settings.MAX_RELEASES_KEPT} 次）",
            ),
            status_code=404,
        )
    view = _view(release)
    return templates.TemplateResponse(
        request, "result.html",
        _ctx(
            request, "",
            page="result",
            release=view,
            run=view["result"],
            overview=report.overview(view),
            publish_blockers=rel.publish_blockers(release, rel.current_rules_digest()),
        ),
    )


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    current = store.load_current()
    views = [_view(r) for r in store.list_releases(limit=50)]
    current_views = [v for v in views if v["is_current"]]
    latest = views[0] if views else None
    return templates.TemplateResponse(
        request, "review.html",
        _ctx(
            request, "/review",
            page="review",
            releases=views,
            release_count=len(views),
            current_views=current_views,
            latest=latest,
            promotions=matrix_table(),
            decimal_rules=DECIMAL_RULES,
            sample_index=schemaio.load_sample_index(),
            max_releases=settings.MAX_RELEASES_KEPT,
        ),
    )
