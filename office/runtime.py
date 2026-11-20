"""Runs a generated project's backend (api.py on miniapi) as a local child process."""
import ast
import asyncio
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .store import Project

TEMPLATES = Path(__file__).parent / "templates"
ALLOWED_IMPORTS = {
    "miniapi", "json", "re", "datetime", "time", "sqlite3", "hashlib", "hmac", "secrets", "uuid", "math",
    "random", "html", "urllib.parse", "typing", "dataclasses", "collections", "itertools", "functools",
    "decimal", "base64", "string", "enum", "statistics", "textwrap", "calendar", "unicodedata",
}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals", "vars"}

_procs: dict[str, tuple[subprocess.Popen, int]] = {}


def audit_api(source: str) -> list[str]:
    """Static guardrail: generated api.py may only use a safe subset of the standard library."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"api.py sintaksis xatosi, {e.lineno}-qator: {e.msg}"]
    problems = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            if name not in ALLOWED_IMPORTS and name.split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"api.py: `{name}` modulini import qilish taqiqlangan (faqat xavfsiz stdlib modullari)")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"api.py: `{node.id}` dan foydalanish taqiqlangan ({node.lineno}-qator)")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            problems.append(f"api.py: `{node.attr}` atributidan foydalanish taqiqlangan ({node.lineno}-qator)")
    if "route" not in source:
        problems.append("api.py: hech qanday @route yo'q")
    return sorted(set(problems))


def install_runtime(project: Project) -> None:
    for name in ("miniapi.py", "server.py"):
        shutil.copyfile(TEMPLATES / name, project.site / name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_of(project: Project) -> int | None:
    entry = _procs.get(project.id)
    if entry and entry[0].poll() is None:
        return entry[1]
    return None


def stop(project_id: str) -> None:
    entry = _procs.pop(project_id, None)
    if entry and entry[0].poll() is None:
        entry[0].terminate()
        try:
            entry[0].wait(3)
        except subprocess.TimeoutExpired:
            entry[0].kill()


def stop_all() -> None:
    for pid in list(_procs):
        stop(pid)


def log_tail(project: Project, lines: int = 30) -> str:
    try:
        return "\n".join((project.dir / "backend.log").read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


async def start(project: Project, fresh_db: bool = False) -> tuple[int | None, str]:
    """(Re)start the backend. Returns (port, "") or (None, error text)."""
    stop(project.id)
    source = project.read_file("api.py")
    if source is None:
        return None, "api.py topilmadi"
    problems = audit_api(source)
    if problems:
        return None, "\n".join(problems)
    install_runtime(project)
    db_path = project.dir / "data.db"
    if fresh_db and db_path.exists():
        db_path.unlink()
    port = _free_port()
    env = {"PATH": os.environ.get("PATH", ""), "PORT": str(port), "HOST": "127.0.0.1", "DB_PATH": str(db_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    log = open(project.dir / "backend.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-S", "server.py"], cwd=project.site, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    log.close()
    _procs[project.id] = (proc, port)
    for _ in range(50):
        if proc.poll() is not None:
            _procs.pop(project.id, None)
            return None, "Server ishga tushmadi:\n" + log_tail(project)
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return port, ""
        except OSError:
            await asyncio.sleep(0.1)
    stop(project.id)
    return None, "Server 5 soniyada javob bermadi:\n" + log_tail(project)
