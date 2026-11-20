"""Real checks the QA agent runs: static analysis, headless Chrome, API smoke test."""
import asyncio
import re
import shutil
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .store import Project

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)
_CONSOLE = re.compile(r':(INFO|WARNING|ERROR|SEVERE):CONSOLE[:(]\d+\)?\]\s*"(.*)", source: (\S*) \((\d+)\)\s*$')
_IGNORED_CONSOLE = ("favicon.ico",)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []
        self.ids: list[str] = []
        self.anchors: list[str] = []
        self.has_title = self.has_viewport = self.has_lang = self.has_description = False
        self.h1 = 0
        self.img_no_alt = 0
        self.buttons_no_type = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "html" and a.get("lang"):
            self.has_lang = True
        if tag == "title":
            self.has_title = True
        if tag == "h1":
            self.h1 += 1
        if tag == "meta" and (a.get("name") or "").lower() == "viewport":
            self.has_viewport = True
        if tag == "meta" and (a.get("name") or "").lower() == "description" and a.get("content"):
            self.has_description = True
        if tag == "img" and "alt" not in a:
            self.img_no_alt += 1
        for attr in ("src", "href"):
            value = (a.get(attr) or "").strip()
            if not value:
                continue
            if value.startswith("#") and len(value) > 1 and tag == "a":
                self.anchors.append(value[1:])
            elif tag in ("script", "link", "img", "a", "source", "iframe", "video", "audio"):
                self.refs.append((tag, value))


def _local_target(base: Path, site: Path, ref: str) -> Path | None:
    ref = ref.split("#")[0].split("?")[0]
    if not ref:
        return None
    return (base.parent / ref).resolve() if not ref.startswith("/") else (site / ref.lstrip("/")).resolve()


def static_checks(project: Project) -> list[dict]:
    """Issues found without running the site. Each: {severity, file, message}."""
    site = project.site.resolve()
    issues: list[dict] = []

    def add(severity: str, file: str, message: str):
        issues.append({"severity": severity, "file": file, "message": message})

    pages = sorted(p for p in site.rglob("*.html") if "docs" not in p.relative_to(site).parts)
    if not (site / "index.html").is_file():
        add("critical", "index.html", "index.html yo'q — saytning bosh sahifasi yaratilmagan")
    for page in pages:
        rel = page.relative_to(site).as_posix()
        parser = _Page()
        try:
            parser.feed(page.read_text(errors="replace"))
        except Exception as e:  # html.parser is lenient; this is a real corruption
            add("high", rel, f"HTML o'qilmadi: {e}")
            continue
        if not parser.has_title:
            add("medium", rel, "<title> yo'q")
        if not parser.has_viewport:
            add("high", rel, "viewport meta tegi yo'q — mobil qurilmalarda noto'g'ri ko'rinadi")
        if not parser.has_lang:
            add("low", rel, '<html> tegida lang atributi yo\'q')
        if not parser.has_description:
            add("low", rel, "meta description yo'q")
        if parser.h1 != 1:
            add("low", rel, f"sahifada {parser.h1} ta <h1> bor (1 ta bo'lishi kerak)")
        if parser.img_no_alt:
            add("medium", rel, f"{parser.img_no_alt} ta <img> da alt atributi yo'q")
        duplicates = sorted({i for i in parser.ids if parser.ids.count(i) > 1})
        if duplicates:
            add("medium", rel, "takrorlangan id lar: " + ", ".join(duplicates[:8]))
        missing_anchors = sorted({a for a in parser.anchors if a not in parser.ids})
        if missing_anchors:
            add("medium", rel, "havola mavjud bo'lmagan id ga olib boradi: #" + ", #".join(missing_anchors[:8]))
        for tag, ref in parser.refs:
            if ref.startswith("/") and not ref.startswith("//"):
                add("high", rel, f"mutlaq yo'l ishlatilgan ({ref}) — nisbiy yo'l bo'lishi kerak")
                continue
            if _EXTERNAL.match(ref) or ref.startswith("api/"):
                continue
            target = _local_target(page, site, ref)
            if target and not target.exists():
                add("high", rel, f"<{tag}> mavjud bo'lmagan faylga ishora qiladi: {ref}")
    for css in site.rglob("*.css"):
        rel = css.relative_to(site).as_posix()
        text = css.read_text(errors="replace")
        for ref in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", text):
            if _EXTERNAL.match(ref):
                continue
            target = _local_target(css, site, ref)
            if target and not target.exists():
                add("medium", rel, f"CSS mavjud bo'lmagan faylga ishora qiladi: {ref}")
        if text.count("{") != text.count("}"):
            add("high", rel, "CSS da ochilgan va yopilgan { } qavslar soni mos emas")
    for js in site.rglob("*.js"):
        rel = js.relative_to(site).as_posix()
        text = js.read_text(errors="replace")
        for m in re.finditer(r"fetch\(\s*['\"`](/[^'\"`]*)", text):
            add("high", rel, f"fetch mutlaq yo'l bilan chaqirilgan ({m.group(1)}) — 'api/...' kabi nisbiy yo'l kerak")
    return issues


async def _chrome(url: str, width: int, height: int, screenshot: Path | None) -> tuple[list[dict], bool]:
    profile = tempfile.mkdtemp(prefix="office-chrome-")
    args = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--mute-audio",
            f"--user-data-dir={profile}", "--enable-logging=stderr", "--virtual-time-budget=6000",
            f"--window-size={width},{height}"]
    args += [f"--screenshot={screenshot}"] if screenshot else ["--dump-dom"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 45)
        except asyncio.TimeoutError:
            proc.kill()
            return [{"level": "ERROR", "message": "Sahifa 45 soniyada yuklanmadi", "source": url, "line": 0}], False
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    logs = []
    for line in err.decode("utf-8", "replace").splitlines():
        m = _CONSOLE.search(line)
        if m and not any(skip in line for skip in _IGNORED_CONSOLE):
            logs.append({"level": m.group(1), "message": m.group(2), "source": m.group(3), "line": int(m.group(4))})
    return logs, True


async def browser_checks(project: Project, base_url: str) -> list[dict]:
    """Open each page in headless Chrome; report console errors and failed loads."""
    if not CHROME:
        return []
    site = project.site
    pages = sorted(p.relative_to(site).as_posix() for p in site.glob("*.html"))[:6]
    issues = []
    for i, page in enumerate(pages):
        shot = project.dir / "screenshot.png" if page == "index.html" else None
        logs, _ = await _chrome(f"{base_url}/{page}", 1280, 900, shot)
        for log in logs:
            text = log["message"]
            is_error = log["level"] in ("ERROR", "SEVERE") or text.startswith("Uncaught")
            if not is_error:
                continue
            source = log["source"].split(f"/sites/{project.id}/")[-1] or page
            issues.append({
                "severity": "critical" if text.startswith("Uncaught") else "high",
                "file": source,
                "message": f"brauzer konsoli ({page}, {log['line']}-qator): {text[:300]}",
            })
    return issues


def api_routes(source: str) -> list[tuple[str, str]]:
    return re.findall(r"@route\(\s*['\"](\w+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", source)


async def api_checks(project: Project, port: int) -> list[dict]:
    """Call every parameter-less GET route; anything 5xx or non-JSON is a bug."""
    source = project.read_file("api.py") or ""
    issues = []
    async with httpx.AsyncClient(timeout=10) as client:
        for method, path in api_routes(source):
            if method.upper() != "GET" or "<" in path:
                continue
            try:
                r = await client.get(f"http://127.0.0.1:{port}{path}")
            except httpx.HTTPError as e:
                issues.append({"severity": "critical", "file": "api.py", "message": f"GET {path} javob bermadi: {e}"})
                continue
            if r.status_code >= 500:
                issues.append({"severity": "critical", "file": "api.py",
                               "message": f"GET {path} → HTTP {r.status_code}. Server logi:\n" + _tail(project)})
            elif r.status_code == 200:
                try:
                    r.json()
                except ValueError:
                    issues.append({"severity": "high", "file": "api.py", "message": f"GET {path} JSON qaytarmadi"})
    return issues


def _tail(project: Project) -> str:
    from . import runtime

    return runtime.log_tail(project, 15)
