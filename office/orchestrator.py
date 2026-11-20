"""Runs the office: the build pipeline and change requests, emitting live events."""
import asyncio
import fnmatch
import json
import logging
import os
import time
import zipfile

from . import checks, llm, runtime
from .agents import AGENTS, BY_ID
from .protocol import Parsed, apply_edit, parse, parse_json
from .store import FileError, Project

LIGHT = {"direktor", "tahlilchi", "kontent", "seo", "deployer"}
FIX_OWNERS = ("baza", "backend", "frontend")
CHANGE_ORDER = ("baza", "backend", "dizayner", "kontent", "frontend", "seo")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
MAX_CONTEXT_FILE = 90_000


class RunError(Exception):
    pass


def base_url(project: Project) -> str:
    return f"http://127.0.0.1:{os.environ.get('OFFICE_PORT', '8000')}/sites/{project.id}"


def _owner_for(path: str) -> str:
    if path.endswith(".py"):
        return "backend"
    if path.endswith(".sql"):
        return "baza"
    return "frontend"


async def together(*coros):
    """Like asyncio.gather, but one failure cancels the rest instead of orphaning them."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class Run:
    def __init__(self, project: Project):
        self.p = project
        self.calls = 0
        self.state: dict[str, str] = {}

    # --- events ---------------------------------------------------------
    def status(self, agent: str, status: str, task: str = "") -> None:
        self.state[agent] = status
        self.p.emit("agent", agent=agent, status=status, task=task)

    def close(self, failed: bool) -> None:
        """Nobody stays 'working' on screen after the run has ended."""
        for agent, status in list(self.state.items()):
            if status in ("working", "waiting"):
                self.status(agent, "error" if failed and status == "working" else "idle")

    def say(self, agent: str, text: str, to: str = "") -> None:
        if text.strip():
            self.p.emit("message", agent=agent, to=to, text=text.strip())

    # --- context --------------------------------------------------------
    def files(self, *patterns: str) -> str:
        """Current project files matching any pattern, formatted for a prompt."""
        blocks = []
        for f in self.p.list_files():
            path = f["path"]
            if patterns and not any(fnmatch.fnmatch(path, pat) for pat in patterns):
                continue
            if path in ("miniapi.py", "server.py"):
                continue
            content = self.p.read_file(path) or ""
            if len(content) > MAX_CONTEXT_FILE:
                content = content[:MAX_CONTEXT_FILE] + "\n[... truncated ...]"
            blocks.append(f"=== CURRENT FILE: {path} ===\n{content}\n=== END OF {path} ===")
        return "\n\n".join(blocks) or "(no files yet)"

    def doc(self, name: str) -> str:
        return self.p.read_file(f"docs/{name}") or "(not written)"

    # --- talking to the model -------------------------------------------
    async def ask(self, agent_id: str, task: str, prompt: str, max_tokens: int = 32000) -> Parsed:
        agent = BY_ID[agent_id]
        self.status(agent_id, "working", task)
        pending: list[str] = []
        last_flush = 0.0

        def flush(force: bool = False) -> None:
            nonlocal last_flush
            if pending and (force or time.monotonic() - last_flush > 0.2):
                self.p.emit_live("delta", agent=agent_id, text="".join(pending))
                pending.clear()
                last_flush = time.monotonic()

        async def on_delta(text: str) -> None:
            pending.append(text)
            flush()

        async def on_reset() -> None:
            pending.clear()
            self.p.emit_live("delta_reset", agent=agent_id)

        async def on_notice(text: str) -> None:
            self.p.emit_live("notice", agent=agent_id, text=text)

        messages = [{"role": "user", "text": prompt}]
        full = ""
        for _ in range(3):
            self.calls += 1
            result = await llm.generate(
                agent.system, messages, max_tokens=max_tokens, light=agent_id in LIGHT,
                on_delta=on_delta, on_reset=on_reset, on_notice=on_notice,
            )
            flush(force=True)
            full += result.text
            if not (result.truncated and parse(full).unterminated):
                break
            messages = [
                {"role": "user", "text": prompt},
                {"role": "assistant", "text": full},
                {"role": "user", "text": "Your output was cut off. Continue exactly from the character where "
                                         "it stopped. Do not repeat anything and do not restart the block."},
            ]
        self.p.emit_live("delta_end", agent=agent_id)
        self.p.emit("llm", agent=agent_id, model=result.model)
        self.p.log_answer(agent_id, task, result.model, full)
        parsed = parse(full)
        parsed.raw = full
        return parsed

    def apply(self, agent_id: str, parsed: Parsed) -> list[str]:
        """Write the agent's files and edits. Returns human-readable failures."""
        agent = BY_ID[agent_id]
        failures = list(parsed.errors)

        def allowed(path: str) -> bool:
            return any(fnmatch.fnmatch(path, pat) for pat in agent.writes)

        for path, content in parsed.files.items():
            try:
                if not allowed(path.strip()):
                    raise FileError(f"{agent.name} {path} faylini yozishga ruxsati yo'q")
                existed = self.p.read_file(path) is not None
                clean = self.p.write_file(path, content)
                self.p.emit("file", agent=agent_id, path=clean, size=len(content.encode()),
                            action="updated" if existed else "created")
            except FileError as e:
                failures.append(str(e))
        for edit in parsed.edits:
            current = self.p.read_file(edit.path)
            if current is None or not allowed(edit.path.strip()):
                failures.append(f"EDIT {edit.path}: fayl topilmadi yoki ruxsat yo'q")
                continue
            updated = apply_edit(current, edit)
            if updated is None:
                failures.append(f"EDIT {edit.path}: FIND matni faylda topilmadi:\n{edit.find[:400]}")
                continue
            self.p.write_file(edit.path, updated)
            self.p.emit("file", agent=agent_id, path=edit.path, size=len(updated.encode()), action="edited")
        return failures

    async def work(self, agent_id: str, task: str, prompt: str, *, required: tuple[str, ...] = ()) -> Parsed:
        """Ask an agent for files, apply them, and give it one chance to repair failed output."""
        parsed = await self.ask(agent_id, task, prompt)
        failures = self.apply(agent_id, parsed)
        failures += [f"{path} fayli yozilmadi" for path in required if self.p.read_file(path) is None]
        if failures:
            paths = sorted({e.path for e in parsed.edits} | set(parsed.failed_paths) | set(required))
            repair = (
                "Some of your output could not be applied:\n- " + "\n- ".join(failures)
                + "\n\nSend the affected files again as complete <<<FILE: path>>> blocks (no EDIT blocks), "
                "with all intended changes included.\n\n" + self.files(*paths)
                + "\n\nYour original task was:\n" + prompt[-6000:]
            )
            again = await self.ask(agent_id, task + " (tuzatish)", repair)
            failures = self.apply(agent_id, again)
            failures += [f"{path} fayli yozilmadi" for path in required if self.p.read_file(path) is None]
            parsed.note = parsed.note or again.note
            if failures and required and any(self.p.read_file(path) is None for path in required):
                raise RunError(f"{BY_ID[agent_id].name} kerakli faylni yoza olmadi: {failures[0]}")
        self.say(agent_id, parsed.note)
        if failures:
            self.say(agent_id, f"Diqqat: {len(failures)} ta o'zgartirishim qo'llanmadi — {failures[0].splitlines()[0]}")
        self.status(agent_id, "done", task)
        return parsed

    async def ask_json(self, agent_id: str, task: str, prompt: str):
        parsed = await self.ask(agent_id, task, prompt, max_tokens=16000)
        data = parse_json(parsed.raw)
        if data is None:
            parsed = await self.ask(
                agent_id, task + " (qayta)",
                prompt + "\n\nYour previous answer was not valid JSON. Answer again, strictly in the required format.",
                max_tokens=16000,
            )
            data = parse_json(parsed.raw)
        if data is None:
            raise RunError(f"{BY_ID[agent_id].name} tushunarsiz javob qaytardi")
        return data, parsed.note

    # --- checks and fixes -------------------------------------------------
    async def run_checks(self, label: str) -> list[dict]:
        self.status("qa", "working", label)
        issues = checks.static_checks(self.p)
        if self.p.meta.get("has_backend"):
            port, error = await runtime.start(self.p, fresh_db=True)
            if error:
                issues.append({"severity": "critical", "file": "api.py", "message": error})
            else:
                issues += await checks.api_checks(self.p, port)
        issues += await checks.browser_checks(self.p, base_url(self.p))
        issues.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 9))
        self.p.emit("check", label=label, issues=issues, browser=bool(checks.CHROME))
        return issues

    async def fix(self, bugs: list[dict], reporter: str, round_label: str) -> None:
        """Send each bug to its owner; owners fix in parallel."""
        by_owner: dict[str, list[dict]] = {}
        for bug in bugs:
            owner = bug.get("owner") if bug.get("owner") in FIX_OWNERS else _owner_for(str(bug.get("file", "")))
            if owner in ("backend", "baza") and not self.p.meta.get("has_backend"):
                owner = "frontend"
            by_owner.setdefault(owner, []).append(bug)

        async def one(owner: str, items: list[dict]) -> None:
            self.say(reporter, f"{len(items)} ta muammo topildi, tuzatish kerak.", to=owner)
            scope = {"frontend": ("*.html", "css/*", "js/*"), "backend": ("api.py", "schema.sql"),
                     "baza": ("schema.sql", "api.py")}[owner]
            listing = "\n".join(
                f"{n}. [{b.get('severity', 'medium')}] {b.get('file', '')}: {b.get('title') or b.get('message', '')}"
                + (f"\n   Details: {b['details']}" if b.get("details") else "")
                + (f"\n   Suggested fix: {b['fix']}" if b.get("fix") else "")
                for n, b in enumerate(items, 1)
            )
            prompt = (
                f"{BY_ID[reporter].name} reported these problems in your work. Fix every one of them with "
                f"precise EDIT blocks (or a full FILE block when a file needs many changes). Do not change "
                f"anything unrelated.\n\nPROBLEMS:\n{listing}\n\n"
                f"API CONTRACT / ARCHITECTURE:\n{self.doc('architecture.json')}\n\n{self.files(*scope)}"
            )
            await self.work(owner, f"{round_label}: {len(items)} ta xatoni tuzatish", prompt)

        await together(*(one(owner, items) for owner, items in by_owner.items()))

    def blocking(self, issues: list[dict]) -> list[dict]:
        return [i for i in issues if i["severity"] in ("critical", "high")]

    # --- deploy -----------------------------------------------------------
    async def publish(self) -> None:
        if self.p.meta.get("has_backend"):
            port, error = await runtime.start(self.p)
            if error:
                self.say("deployer", "Backend ishga tushmadi: " + error.splitlines()[0])
        version = int(self.p.meta.get("version", 0)) + 1
        self.p.update(version=version)
        build_zip(self.p)
        self.p.emit("preview", version=version, url=f"sites/{self.p.id}/")

    # --- pipelines ----------------------------------------------------------
    async def build(self) -> None:
        p = self.p
        brief = p.meta["brief"]

        plan, note = await self.ask_json(
            "direktor", "Vazifani tahlil qilish va taqsimlash",
            f"USER REQUEST:\n{brief}\n\n"
            "Define the project and brief the team. JSON schema:\n"
            '{"name": "short product name, 2-4 words", "language": "language of the site copy (match the '
            'user\'s request language unless they ask otherwise)", "kind": "landing | multi-page site | web app | '
            'tool | game", "summary": "3-4 sentences: what we build, for whom, the must-have features", '
            '"assignments": {"tahlilchi": "...", "arxitektor": "...", "dizayner": "...", "kontent": "...", '
            '"baza": "...", "backend": "...", "frontend": "...", "qa": "...", "xavfsizlik": "...", "seo": "...", '
            '"deployer": "..."}}\n'
            "Each assignment is one specific sentence in Uzbek for that teammate. Keep the scope achievable: "
            "one polished product, not a platform.",
        )
        if not isinstance(plan, dict):
            raise RunError("Direktor rejasi noto'g'ri formatda")
        p.update(name=str(plan.get("name") or "Yangi loyiha")[:60])
        p.emit("project", name=p.meta["name"])
        self.say("direktor", note or str(plan.get("summary", "")))
        assignments = plan.get("assignments") if isinstance(plan.get("assignments"), dict) else {}
        for agent in AGENTS[1:]:
            if assignments.get(agent.id):
                self.say("direktor", str(assignments[agent.id]), to=agent.id)
                self.status(agent.id, "waiting", str(assignments[agent.id]))
        self.status("direktor", "done", "Reja tayyor")
        header = (
            f"PROJECT: {plan.get('name')}\nKIND: {plan.get('kind')}\nSITE LANGUAGE: {plan.get('language')}\n"
            f"SUMMARY: {plan.get('summary')}\n\nUSER REQUEST (verbatim):\n{brief}\n"
        )

        def mine(agent_id: str) -> str:
            return f"YOUR ASSIGNMENT FROM DIREKTOR: {assignments.get(agent_id, 'do your role for this project')}\n"

        await self.work(
            "tahlilchi", "Texnik topshiriq yozish",
            f"{header}\n{mine('tahlilchi')}\nWrite the specification as docs/spec.md. Describe behaviour, not storage "
            "technology: Arxitektor decides between localStorage and a server database.",
            required=("docs/spec.md",),
        )
        spec = self.doc("spec.md")

        async def architecture() -> dict:
            arch, arch_note = await self.ask_json(
                "arxitektor", "Arxitektura va API shartnomasi",
                f"{header}\n{mine('arxitektor')}\nSPECIFICATION:\n{spec}\n\n"
                "Design the architecture. JSON schema:\n"
                '{"needs_backend": true|false, "reason": "why", "files": [{"path": "index.html", "purpose": "..."}], '
                '"storage": "what is stored and where (localStorage keys or database tables)", '
                '"api": [{"method": "GET", "path": "/api/...", "request": "body/query fields", "response": "exact '
                'JSON shape", "errors": "status codes"}], "data_model": [{"table": "...", "columns": ["name TYPE ..."]}], '
                '"frontend_notes": "state management, modules, key functions"}\n'
                "You own the storage decision, whatever the spec or assignments say. needs_backend=true whenever "
                "data must be shared between visitors or reach the site owner: bookings, orders, contact or "
                "application forms, comments, admin lists. A form whose submissions stay in the visitor's own "
                "localStorage is useless to the owner. A landing page without such forms, a portfolio, calculator, "
                "game or personal tool uses localStorage and needs_backend=false with empty api and data_model. "
                "Front-end files: index.html, css/style.css, js/app.js, plus extra pages only if the spec needs them. "
                "With a backend also list schema.sql and api.py. The front end calls the API with relative URLs "
                "like fetch('api/items').",
            )
            if not isinstance(arch, dict):
                raise RunError("Arxitektor javobi noto'g'ri formatda")
            p.write_file("docs/architecture.json", json.dumps(arch, ensure_ascii=False, indent=2))
            p.emit("file", agent="arxitektor", path="docs/architecture.json", size=0, action="created")
            self.say("arxitektor", arch_note or str(arch.get("reason", "")))
            self.status("arxitektor", "done", "Arxitektura tayyor")
            return arch

        arch, _, _ = await together(
            architecture(),
            self.work(
                "dizayner", "Dizayn tizimi va maket",
                f"{header}\n{mine('dizayner')}\nSPECIFICATION:\n{spec}\n\n"
                "Write the design system and page layouts as docs/design.md (do not write CSS files now; the "
                "front-end developer implements your design). Include exact hex colours, font families "
                "(Google Fonts names), type scale, spacing, radii, shadows, each section's layout at 360px and "
                "1280px, component styles and states, and the motion you want.",
                required=("docs/design.md",),
            ),
            self.work(
                "kontent", "Sayt matnlarini yozish",
                f"{header}\n{mine('kontent')}\nSPECIFICATION:\n{spec}\n\n"
                "Write every piece of text for the product as docs/content.md, organised by page and section "
                "(do not write HTML files now). Include realistic sample data where the product needs it.",
                required=("docs/content.md",),
            ),
        )
        has_backend = bool(arch.get("needs_backend"))
        p.update(has_backend=has_backend)
        arch_text = self.doc("architecture.json")

        async def server_side() -> None:
            if not has_backend:
                reason = "Bu loyihaga server kerak emas"
                self.status("baza", "skipped", reason)
                self.status("backend", "skipped", reason)
                return
            runtime.install_runtime(p)
            await self.work(
                "baza", "Ma'lumotlar bazasi sxemasi",
                f"{header}\n{mine('baza')}\nARCHITECTURE:\n{arch_text}\n\nSAMPLE CONTENT (for seed rows):\n"
                f"{self.doc('content.md')[:6000]}\n\nWrite schema.sql.",
                required=("schema.sql",),
            )
            await self.work(
                "backend", "API yozish",
                f"{header}\n{mine('backend')}\nARCHITECTURE (implement this API contract exactly):\n{arch_text}\n\n"
                f"{self.files('schema.sql')}\n\nWrite api.py.",
                required=("api.py",),
            )

        await together(
            server_side(),
            self.work(
                "frontend", "Sayt kodini yozish",
                f"{header}\n{mine('frontend')}\nSPECIFICATION:\n{spec}\n\nARCHITECTURE:\n{arch_text}\n\n"
                f"DESIGN SYSTEM:\n{self.doc('design.md')}\n\nCOPY:\n{self.doc('content.md')}\n\n"
                "Write all front-end files listed in the architecture, complete and working. "
                + ("Call the API with relative URLs exactly as the contract defines and handle failures. "
                   if has_backend else "There is no backend: keep state in localStorage. ")
                + "Start with index.html, then css/style.css, then js/app.js.",
                required=("index.html",),
            ),
        )

        # QA: automated checks, then a code review with those results in hand
        issues = await self.run_checks("Avtomatik tekshiruv")
        report, note = await self.ask_json(
            "qa", "Kodni ko'rib chiqish va xatolarni topish",
            f"{header}\nSPECIFICATION:\n{spec}\n\nARCHITECTURE:\n{arch_text}\n\n"
            f"AUTOMATED CHECK RESULTS ({'static analysis + headless Chrome' if checks.CHROME else 'static analysis only'}):\n"
            f"{json.dumps(issues, ensure_ascii=False, indent=1) if issues else 'no issues found'}\n\n"
            f"{self.files('*.html', 'css/*', 'js/*', 'api.py', 'schema.sql')}\n\n"
            "Review the code and report the bugs that matter most (at most 8, real defects only, automated "
            "findings included when they are real). JSON schema:\n"
            '{"verdict": "pass" | "fail", "bugs": [{"severity": "critical|high|medium|low", "owner": '
            '"frontend|backend|baza", "file": "path", "title": "short title in Uzbek", "details": "what is wrong '
            'and where (quote the code)", "fix": "exact change to make"}]}',
        )
        bugs = [b for b in (report.get("bugs") if isinstance(report, dict) else []) or [] if isinstance(b, dict)][:8]
        p.emit("bugs", agent="qa", bugs=bugs)
        self.say("qa", note or (f"{len(bugs)} ta xato topildi." if bugs else "Jiddiy xato topilmadi."))
        self.status("qa", "done", f"{len(bugs)} ta xato topildi")
        if bugs:
            await self.fix(bugs, "qa", "QA")
            remaining = self.blocking(await self.run_checks("Qayta tekshiruv"))
            if remaining:
                await self.fix(remaining, "qa", "QA 2-bosqich")
            self.status("qa", "done", "Tekshiruv tugadi")

        # Security review
        audit, note = await self.ask_json(
            "xavfsizlik", "Xavfsizlik auditi",
            f"{header}\nARCHITECTURE:\n{arch_text}\n\n{self.files('*.html', 'js/*', 'api.py', 'schema.sql')}\n\n"
            "Audit this code. Report at most 6 real, exploitable issues. JSON schema:\n"
            '{"issues": [{"severity": "critical|high|medium|low", "owner": "frontend|backend|baza", "file": "path", '
            '"title": "short title in Uzbek", "details": "the vulnerable code and how it is exploited", '
            '"fix": "exact change to make"}]}',
        )
        vulns = [v for v in (audit.get("issues") if isinstance(audit, dict) else []) or [] if isinstance(v, dict)][:6]
        p.emit("bugs", agent="xavfsizlik", bugs=vulns)
        self.say("xavfsizlik", note or (f"{len(vulns)} ta zaiflik topildi." if vulns else "Zaiflik topilmadi."))
        self.status("xavfsizlik", "done", f"{len(vulns)} ta zaiflik topildi")
        to_fix = [v for v in vulns if v.get("severity") in ("critical", "high", "medium")]
        if to_fix:
            await self.fix(to_fix, "xavfsizlik", "Xavfsizlik")

        # SEO, performance, accessibility polish
        await self.work(
            "seo", "SEO, tezlik va qulaylikni sozlash",
            f"{header}\n{mine('seo')}\n{self.files('*.html')}\n\n"
            "Optimise these pages with EDIT blocks and add robots.txt (allow all).",
        )

        # Final regression pass before going live
        remaining = self.blocking(await self.run_checks("Yakuniy tekshiruv"))
        if remaining:
            await self.fix(remaining, "qa", "Yakuniy tuzatish")
            remaining = self.blocking(await self.run_checks("Yakuniy tekshiruv (2)"))
        self.status("qa", "done", "Yakuniy tekshiruv: " + (f"{len(remaining)} ta muammo qoldi" if remaining else "toza"))

        stack = (
            "Front end: static HTML/CSS/JS. Back end: api.py on the bundled miniapi.py (Python 3 standard "
            "library, SQLite, schema.sql applied on start). Run with `python3 server.py` and open "
            "http://127.0.0.1:8000 ; PORT and HOST env vars are respected; the database file is data.db."
            if has_backend else
            "Static site only (HTML/CSS/JS, no build step, no server code). Run locally with "
            "`python3 -m http.server 8000` in the project folder and open http://127.0.0.1:8000 ; deploy by "
            "uploading the folder to any static host (GitHub Pages, Netlify, Vercel, Cloudflare Pages)."
        )
        await self.work(
            "deployer", "README yozish va jonli ishga tushirish",
            f"{header}\n{mine('deployer')}\nSTACK (facts): {stack}\n\nFILES:\n"
            + "\n".join(f"- {f['path']} ({f['size']} bytes)" for f in p.list_files())
            + f"\n\nKNOWN REMAINING ISSUES: {json.dumps(remaining, ensure_ascii=False) if remaining else 'none'}\n\n"
            "Write README.md in the site language. In your NOTE, announce to the user that the project is live "
            "and summarise what was built in two sentences.",
            required=("README.md",),
        )
        await self.publish()

    async def change(self, request: str) -> None:
        p = self.p
        has_backend = bool(p.meta.get("has_backend"))
        candidates = [a for a in CHANGE_ORDER if has_backend or a not in ("baza", "backend")]
        plan, note = await self.ask_json(
            "direktor", "O'zgartirish so'rovini taqsimlash",
            f"PROJECT: {p.meta['name']}\nORIGINAL REQUEST: {p.meta['brief']}\n\n"
            f"THE USER NOW ASKS FOR THIS CHANGE:\n{request}\n\nPROJECT FILES:\n"
            + "\n".join(f"- {f['path']} ({f['size']} bytes)" for f in p.list_files())
            + f"\n\nSPECIFICATION:\n{self.doc('spec.md')[:5000]}\n\n"
            f"Decide which teammates must act. Available: {', '.join(candidates)}. "
            "frontend edits HTML/CSS/JS; kontent edits texts in HTML; dizayner edits css/*.css; "
            "baza edits schema.sql; backend edits api.py; seo edits meta tags. Use as few as possible "
            "(usually only frontend). JSON schema:\n"
            '{"summary": "one sentence in Uzbek", "assignments": {"agent_id": "precise instruction in Uzbek"}}',
        )
        assignments = plan.get("assignments") if isinstance(plan, dict) and isinstance(plan.get("assignments"), dict) else {}
        assignments = {a: str(t) for a, t in assignments.items() if a in candidates and t}
        if not assignments:
            assignments = {"frontend": request}
        self.say("direktor", note or str(plan.get("summary", "")))
        for agent_id, text in assignments.items():
            self.say("direktor", text, to=agent_id)
            self.status(agent_id, "waiting", text)
        self.status("direktor", "done", "Topshiriqlar berildi")
        scopes = {
            "baza": ("schema.sql", "api.py"), "backend": ("api.py", "schema.sql"), "dizayner": ("css/*", "*.html"),
            "kontent": ("*.html",), "frontend": ("*.html", "css/*", "js/*"), "seo": ("*.html",),
        }
        for agent_id in CHANGE_ORDER:
            if agent_id not in assignments:
                continue
            await self.work(
                agent_id, assignments[agent_id][:80],
                f"PROJECT: {p.meta['name']}\nTHE USER ASKED FOR THIS CHANGE:\n{request}\n\n"
                f"YOUR ASSIGNMENT FROM DIREKTOR: {assignments[agent_id]}\n\n"
                f"ARCHITECTURE:\n{self.doc('architecture.json')}\n\n{self.files(*scopes[agent_id])}\n\n"
                "Make the change in the existing files with EDIT blocks (or a full FILE block when a file needs "
                "many changes). Keep everything else working.",
            )
        remaining = self.blocking(await self.run_checks("O'zgarishdan keyingi tekshiruv"))
        if remaining:
            await self.fix(remaining, "qa", "Tuzatish")
            remaining = self.blocking(await self.run_checks("Qayta tekshiruv"))
        self.say("qa", "Avtomatik tekshiruv: " + (f"{len(remaining)} ta muammo qoldi." if remaining else "muammo topilmadi."))
        self.status("qa", "done", "Tekshiruv tugadi")
        self.status("deployer", "working", "Yangi versiyani ishga tushirish")
        await self.publish()
        self.say("deployer", f"Yangi versiya (v{p.meta['version']}) jonli ko'rinishda.")
        self.status("deployer", "done", "Jonli")


def build_zip(project: Project) -> None:
    target = project.dir / "site.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(project.site.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".db", ".pyc"):
                z.write(f, f.relative_to(project.site).as_posix())


async def _execute(project: Project, kind: str, request: str) -> None:
    run = Run(project)
    project.update(status="running")
    project.emit("run_start", kind=kind, request=request)
    try:
        if kind == "build":
            await run.build()
        else:
            await run.change(request)
    except asyncio.CancelledError:
        run.close(failed=False)
        project.update(status="stopped")
        project.emit("run_end", status="stopped", message="Ish to'xtatildi.")
        raise
    except (llm.LLMError, RunError, FileError) as e:
        run.close(failed=True)
        project.update(status="error")
        project.emit("run_end", status="error", message=str(e))
    except Exception as e:  # keep the office alive and show the user what broke
        logging.getLogger("office").exception("run failed")
        run.close(failed=True)
        project.update(status="error")
        project.emit("run_end", status="error", message=f"Kutilmagan xato: {type(e).__name__}: {e}")
    else:
        run.close(failed=False)
        project.update(status="done")
        project.emit("run_end", status="done", message=f"Tayyor. AI so'rovlari: {run.calls} ta.")


def start(project: Project, kind: str, request: str) -> None:
    if project.running:
        raise RunError("Bu loyihada agentlar allaqachon ishlayapti")
    project.task = asyncio.create_task(_execute(project, kind, request))
