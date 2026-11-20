"""OpenAI-compatible fake model for testing the pipeline offline.

    python tests/mock_llm.py 9911
    OFFICE_PROVIDER=openai OPENAI_API_KEY=x OPENAI_BASE_URL=http://127.0.0.1:9911/v1 \
        OFFICE_MODELS=mock OFFICE_DATA_DIR=/tmp/office-test python -m office --port 8765

It plays every agent: the first front-end version contains a JavaScript error that QA must
catch in headless Chrome and the front-end agent must fix with an EDIT block.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOTE = "<<<NOTE>>>\n{}\n<<<END>>>\n"
INDEX = """<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sinov</title>
<link rel="stylesheet" href="css/style.css">
</head>
<body>
<h1>Eslatmalar</h1>
<form id="form"><input id="title" aria-label="Eslatma"><button type="submit">Qo'shish</button></form>
<ul id="list"></ul>
<script src="js/app.js"></script>
</body>
</html>"""
APP_JS = """document.addEventListener('DOMContentLoaded', async () => {
  const list = document.getElementById('list');
  const render = (items) => { list.textContent = ''; items.forEach((i) => { const li = document.createElement('li'); li.textContent = i.title; list.append(li); }); };
  render(await (await fetch('api/notes')).json());
  brokenCall();
  document.getElementById('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    await fetch('api/notes', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: document.getElementById('title').value }) });
    render(await (await fetch('api/notes')).json());
  });
});"""
API_PY = '''from miniapi import route, HTTPError


@route("GET", "/api/notes")
def list_notes(req):
    return [dict(r) for r in req.db.execute("SELECT * FROM notes ORDER BY id").fetchall()]


@route("POST", "/api/notes")
def add_note(req):
    title = str((req.json or {}).get("title", "")).strip()[:200]
    if not title:
        raise HTTPError(400, "title is required")
    cur = req.db.execute("INSERT INTO notes (title) VALUES (?)", (title,))
    return 201, {"id": cur.lastrowid, "title": title}
'''
SCHEMA = """CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
INSERT OR IGNORE INTO notes (id, title) VALUES (1, 'Birinchi eslatma');"""


def file(path, content):
    return f"<<<FILE: {path}>>>\n{content}\n<<<END>>>\n"


def answer(system: str, user: str) -> str:
    def who(name):
        return f"You are {name}," in system

    if who("Direktor"):
        if "NOW ASKS FOR THIS CHANGE" in user:
            data = {"summary": "Sarlavhani o'zgartiramiz", "assignments": {"frontend": "Sarlavhani yangilang"}}
        else:
            agents = "tahlilchi arxitektor dizayner kontent baza backend frontend qa xavfsizlik seo deployer".split()
            data = {"name": "Eslatmalar", "language": "uz", "kind": "web app", "summary": "Eslatmalar ilovasi.",
                    "assignments": {a: f"{a} uchun topshiriq" for a in agents}}
        return NOTE.format("Reja tayyor.") + "<<<JSON>>>\n" + json.dumps(data) + "\n<<<END>>>\n"
    if who("Tahlilchi"):
        return NOTE.format("Spec yozildi.") + file("docs/spec.md", "# Spec\n- Eslatma qo'shish\n- Ro'yxat")
    if who("Arxitektor"):
        data = {"needs_backend": True, "reason": "umumiy ma'lumot", "files": [{"path": "index.html", "purpose": "ui"}],
                "api": [{"method": "GET", "path": "/api/notes"}, {"method": "POST", "path": "/api/notes"}]}
        return NOTE.format("Backend kerak.") + "```json\n" + json.dumps(data) + "\n```"
    if who("Dizayner"):
        return NOTE.format("Dizayn tayyor.") + file("docs/design.md", "# Dizayn\n- rang: #317CFF")
    if who("Kontent yozuvchi"):
        return NOTE.format("Matnlar tayyor.") + file("docs/content.md", "# Matn\nEslatmalar")
    if who("Baza muhandisi"):
        return NOTE.format("Sxema tayyor.") + file("schema.sql", SCHEMA)
    if who("Backend dasturchi"):
        return NOTE.format("API tayyor.") + file("api.py", API_PY)
    if who("Frontend dasturchi"):
        if "reported these problems" in user:
            return (NOTE.format("Xato tuzatildi.") + "<<<EDIT: js/app.js>>>\n<<<FIND>>>\n  brokenCall();\n"
                    "<<<REPLACE>>>\n<<<END>>>\n")
        if "ASKED FOR THIS CHANGE" in user:
            return (NOTE.format("Sarlavha yangilandi.") + "<<<EDIT: index.html>>>\n<<<FIND>>>\n<h1>Eslatmalar</h1>\n"
                    "<<<REPLACE>>>\n<h1>Mening eslatmalarim</h1>\n<<<END>>>\n")
        return (NOTE.format("Sayt yozildi.") + file("index.html", INDEX)
                + file("css/style.css", "body { font-family: sans-serif; margin: 2rem; }") + file("js/app.js", APP_JS))
    if who("Xato qidiruvchi"):
        bugs = []
        if "brokenCall is not defined" in user:
            bugs = [{"severity": "critical", "owner": "frontend", "file": "js/app.js", "title": "brokenCall aniqlanmagan",
                     "details": "brokenCall() chaqirilgan", "fix": "qatorni o'chirish"}]
        return NOTE.format(f"{len(bugs)} ta xato.") + "<<<JSON>>>\n" + json.dumps({"verdict": "fail", "bugs": bugs}) + "\n<<<END>>>\n"
    if who("Xavfsizlik auditori"):
        return NOTE.format("Zaiflik topilmadi.") + '<<<JSON>>>\n{"issues": []}\n<<<END>>>\n'
    if who("SEO optimizator"):
        return (NOTE.format("Meta teglar qo'shildi.") + "<<<EDIT: index.html>>>\n<<<FIND>>>\n<title>Sinov</title>\n<<<REPLACE>>>\n"
                '<title>Eslatmalar</title>\n<meta name="description" content="Eslatmalar ilovasi">\n<<<END>>>\n'
                + file("robots.txt", "User-agent: *\nAllow: /"))
    return NOTE.format("Loyiha jonli.") + file("README.md", "# Eslatmalar\n\n`python3 server.py`")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        text = answer(system, body["messages"][-1]["content"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(0, len(text), 40):
            chunk = {"choices": [{"delta": {"content": text[i:i + 40]}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9911), Handler).serve_forever()
