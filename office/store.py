"""Projects on disk: metadata, generated files and the event log that drives the live UI."""
import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

from . import config

PROJECTS_DIR = config.DATA / "projects"
WRITABLE_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md", ".xml", ".webmanifest", ".py", ".sql"}
RESERVED = {"server.py", "miniapi.py"}
PRIVATE_EXT = {".py", ".sql", ".db", ".log"}
MAX_FILE_BYTES = 400_000


class FileError(Exception):
    pass


def clean_path(path: str) -> str:
    path = path.strip().strip("`'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    p = PurePosixPath(path)
    if not path or p.is_absolute() or any(part in ("..", "") or part.startswith(".") for part in p.parts):
        raise FileError(f"yaroqsiz fayl yo'li: {path!r}")
    if not re.fullmatch(r"[A-Za-z0-9_\-./]+", path) or len(path) > 120:
        raise FileError(f"yaroqsiz fayl nomi: {path!r}")
    if p.suffix.lower() not in WRITABLE_EXT:
        raise FileError(f"bu turdagi faylni yozib bo'lmaydi: {path}")
    if str(p) in RESERVED:
        raise FileError(f"{path} tizim fayli, uni o'zgartirib bo'lmaydi")
    return str(p)


class Project:
    def __init__(self, meta: dict):
        self.meta = meta
        self.id: str = meta["id"]
        self.dir = PROJECTS_DIR / self.id
        self.site = self.dir / "site"
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self._log = self.dir / "events.jsonl"

    # --- metadata -------------------------------------------------------
    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "project.tmp"
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2))
        tmp.replace(self.dir / "project.json")

    def update(self, **fields) -> None:
        self.meta.update(fields)
        self.save()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    # --- events ---------------------------------------------------------
    def emit(self, type_: str, **data) -> dict:
        """Persisted event, replayed to clients that connect later."""
        event = {"seq": len(self.events) + 1, "ts": time.time(), "type": type_, **data}
        self.events.append(event)
        with self._log.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._publish(event)
        return event

    def emit_live(self, type_: str, **data) -> None:
        """Transient event (typing stream); only connected clients see it."""
        self._publish({"type": type_, **data})

    def log_answer(self, agent: str, task: str, model: str, text: str) -> None:
        """Keep each raw model answer next to the project, for debugging a build after the fact."""
        with (self.dir / "answers.log").open("a") as f:
            f.write(f"\n===== {time.strftime('%H:%M:%S')} {agent} | {task} | {model} =====\n{text}\n")

    def _publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(event)

    # --- files ----------------------------------------------------------
    def write_file(self, path: str, content: str) -> str:
        path = clean_path(path)
        data = content.encode()
        if len(data) > MAX_FILE_BYTES:
            raise FileError(f"{path} juda katta ({len(data)} bayt)")
        target = self.site / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return path

    def read_file(self, path: str) -> str | None:
        try:
            return (self.site / clean_path(path)).read_text(errors="replace")
        except (OSError, FileError):
            return None

    def list_files(self) -> list[dict]:
        if not self.site.exists():
            return []
        out = []
        for p in sorted(self.site.rglob("*")):
            rel = p.relative_to(self.site).as_posix()
            if p.is_file() and p.suffix.lower() in WRITABLE_EXT and not rel.startswith("."):
                out.append({"path": rel, "size": p.stat().st_size})
        return out


_projects: dict[str, Project] = {}


def load_all() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    for meta_path in PROJECTS_DIR.glob("*/project.json"):
        try:
            project = Project(json.loads(meta_path.read_text()))
        except (OSError, ValueError, KeyError):
            continue
        if project._log.exists():
            for line in project._log.read_text().splitlines():
                try:
                    project.events.append(json.loads(line))
                except ValueError:
                    pass
        if project.meta.get("status") == "running":
            project.update(status="stopped")
            project.emit("run_end", status="stopped", message="Server qayta ishga tushdi, ish to'xtatildi.")
        _projects[project.id] = project


def create(brief: str) -> Project:
    pid = uuid.uuid4().hex[:10]
    project = Project(
        {"id": pid, "name": "Yangi loyiha", "brief": brief, "created": time.time(), "status": "idle",
         "version": 0, "has_backend": False}
    )
    project.site.mkdir(parents=True, exist_ok=True)
    project.save()
    _projects[pid] = project
    return project


def get(pid: str) -> Project | None:
    return _projects.get(pid)


def all_projects() -> list[Project]:
    return sorted(_projects.values(), key=lambda p: p.meta["created"], reverse=True)


def delete(pid: str) -> None:
    project = _projects.pop(pid, None)
    if project:
        shutil.rmtree(project.dir, ignore_errors=True)
