"""HTTP API, live event stream and hosting of the generated sites."""
import asyncio
import json
import mimetypes
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import checks, config, llm, orchestrator, runtime, store
from .agents import public_roster


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.load_all()
    yield
    for project in store.all_projects():
        if project.running:
            project.task.cancel()
    runtime.stop_all()


app = FastAPI(title="AI Agent Office", lifespan=lifespan, docs_url=None, redoc_url=None)
NO_STORE = {"Cache-Control": "no-store"}


def project_or_404(pid: str) -> store.Project:
    project = store.get(pid) if re.fullmatch(r"[0-9a-f]{10}", pid) else None
    if not project:
        raise HTTPException(404, "Loyiha topilmadi")
    return project


def summary(project: store.Project) -> dict:
    return {**project.meta, "running": project.running, "has_screenshot": (project.dir / "screenshot.png").exists()}


class Brief(BaseModel):
    text: str = Field(min_length=3, max_length=4000)


class Settings(BaseModel):
    provider: str = "gemini"
    api_key: str = Field(default="", max_length=400)
    models: str = Field(default="", max_length=400)
    base_url: str = Field(default="", max_length=300)


# --- office ---------------------------------------------------------------
@app.get("/api/state")
async def state():
    return {
        "agents": public_roster(),
        "settings": config.public(),
        "projects": [summary(p) for p in store.all_projects()],
        "browser_checks": bool(checks.CHROME),
    }


@app.post("/api/settings")
async def save_settings(body: Settings):
    models = [m.strip() for m in body.models.split(",") if m.strip()]
    config.save(body.provider, body.api_key or None, models, body.base_url)
    cfg = config.load()
    result = {"settings": config.public(), "ok": True, "message": "Saqlandi."}
    if cfg["provider"] == "gemini" and cfg["api_key"]:
        try:
            async with httpx.AsyncClient() as client:
                llm._gemini_models = None
                heavy, light = await llm.gemini_models(client, cfg["api_key"])
            result["message"] = f"Kalit ishlayapti. Modellar: {', '.join((models or heavy + light)[:4])}…"
        except (llm.LLMError, httpx.HTTPError) as e:
            result.update(ok=False, message=str(e) or "Kalitni tekshirib bo'lmadi")
    return result


@app.post("/api/projects")
async def create_project(body: Brief):
    if not config.load()["api_key"]:
        raise HTTPException(400, "Avval sozlamalarda AI API kalitini kiriting.")
    project = store.create(body.text.strip())
    orchestrator.start(project, "build", body.text.strip())
    return summary(project)


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    project = project_or_404(pid)
    return {**summary(project), "files": project.list_files()}


@app.post("/api/projects/{pid}/change")
async def change_project(pid: str, body: Brief):
    project = project_or_404(pid)
    if project.running:
        raise HTTPException(409, "Agentlar hozir ishlayapti. Tugashini kuting yoki to'xtating.")
    if not project.read_file("index.html"):
        raise HTTPException(400, "Bu loyiha hali qurilmagan. Avval qayta quring.")
    orchestrator.start(project, "change", body.text.strip())
    return summary(project)


@app.post("/api/projects/{pid}/rebuild")
async def rebuild_project(pid: str):
    project = project_or_404(pid)
    if project.running:
        raise HTTPException(409, "Agentlar hozir ishlayapti.")
    orchestrator.start(project, "build", project.meta["brief"])
    return summary(project)


@app.post("/api/projects/{pid}/stop")
async def stop_project(pid: str):
    project = project_or_404(pid)
    if project.running:
        project.task.cancel()
    return {"ok": True}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    project = project_or_404(pid)
    if project.running:
        project.task.cancel()
    runtime.stop(pid)
    store.delete(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/events")
async def events(pid: str, request: Request, after: int = 0):
    project = project_or_404(pid)

    async def stream():
        queue: asyncio.Queue = asyncio.Queue()
        project.subscribers.add(queue)
        try:
            sent = after
            for event in list(project.events):
                if event["seq"] > sent:
                    sent = event["seq"]
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'ready', 'running': project.running})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                if event.get("seq", sent + 1) <= sent:
                    continue
                sent = event.get("seq", sent)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            project.subscribers.discard(queue)

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@app.get("/api/projects/{pid}/file")
async def read_file(pid: str, path: str):
    content = project_or_404(pid).read_file(path)
    if content is None:
        raise HTTPException(404, "Fayl topilmadi")
    return {"path": path, "content": content}


@app.get("/api/projects/{pid}/zip")
async def download_zip(pid: str):
    project = project_or_404(pid)
    if not project.list_files():
        raise HTTPException(404, "Fayllar hali yo'q")
    orchestrator.build_zip(project)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", project.meta["name"]).strip("-") or "loyiha"
    return FileResponse(project.dir / "site.zip", filename=f"{name}.zip", media_type="application/zip")


@app.get("/api/projects/{pid}/screenshot")
async def screenshot(pid: str):
    path = project_or_404(pid).dir / "screenshot.png"
    if not path.exists():
        raise HTTPException(404, "Skrinshot yo'q")
    return FileResponse(path, media_type="image/png", headers=NO_STORE)


# --- generated sites --------------------------------------------------------
@app.api_route("/sites/{pid}/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def site_api(pid: str, path: str, request: Request):
    project = project_or_404(pid)
    if not project.meta.get("has_backend"):
        raise HTTPException(404, "Bu loyihada API yo'q")
    port = runtime.port_of(project)
    if port is None:
        port, error = await runtime.start(project)
        if error:
            return Response(json.dumps({"error": "backend ishga tushmadi"}), 502, media_type="application/json")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "So'rov juda katta")
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            upstream = await client.request(
                request.method, f"http://127.0.0.1:{port}/api/{path}", params=request.query_params,
                content=body, headers=headers,
            )
    except httpx.HTTPError:
        return Response(json.dumps({"error": "backend javob bermadi"}), 502, media_type="application/json")
    return Response(upstream.content, upstream.status_code, headers=NO_STORE,
                    media_type=upstream.headers.get("content-type", "application/json"))


@app.get("/sites/{pid}")
async def site_root(pid: str):
    project_or_404(pid)
    return RedirectResponse(f"/sites/{pid}/")


@app.get("/sites/{pid}/{path:path}")
async def site_file(pid: str, path: str):
    project = project_or_404(pid)
    root = project.site.resolve()
    target = (root / (path or "index.html")).resolve()
    if target.is_dir():
        target = target / "index.html"
    hidden = target.suffix.lower() in store.PRIVATE_EXT or any(part.startswith(".") for part in target.parts[len(root.parts):])
    if root not in target.parents or not target.is_file() or hidden:
        raise HTTPException(404, "Sahifa topilmadi")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers=NO_STORE)


# --- office UI ----------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")
