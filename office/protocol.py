"""The plain-text protocol agents answer in, and how their file edits are applied."""
import json
import re
from dataclasses import dataclass, field

FORMAT_HELP = """\
OUTPUT FORMAT (strict, plain text, no markdown fences around blocks):

<<<NOTE>>>
One to three short sentences in Uzbek for your teammates: what you did or decided.
<<<END>>>

To create or fully replace a file:
<<<FILE: relative/path.ext>>>
complete file content
<<<END>>>

To change part of an existing file (preferred for small fixes):
<<<EDIT: relative/path.ext>>>
<<<FIND>>>
exact text copied from the current file (enough lines to be unique)
<<<REPLACE>>>
new text
<<<END>>>

Rules: every block ends with <<<END>>> on its own line. Never abbreviate file content with
placeholders like "..." or "rest of file". Paths are relative, no leading slash."""

JSON_HELP = """\
OUTPUT FORMAT (strict):

<<<NOTE>>>
One to three short sentences in Uzbek for your teammates.
<<<END>>>
<<<JSON>>>
a single valid JSON value matching the requested schema
<<<END>>>"""

# Models regularly drop a bracket ("<<<FIND>", "<REPLACE>"), so markers are matched leniently.
_HEADER = re.compile(r"^\s*<{2,3}\s*(NOTE|JSON|FILE|EDIT)\s*(?::\s*(.+?))?\s*>{1,3}\s*$")
_END = re.compile(r"^\s*<{2,3}\s*END\s*>{1,3}\s*$")
_FIND = re.compile(r"^\s*<{1,3}\s*FIND\s*>{1,3}\s*$")
_REPLACE = re.compile(r"^\s*<{1,3}\s*REPLACE\s*>{1,3}\s*$")
_FENCE = re.compile(r"^\s*```[\w+-]*\s*$")


@dataclass
class Edit:
    path: str
    find: str
    replace: str


@dataclass
class Parsed:
    note: str = ""
    json_text: str = ""
    files: dict[str, str] = field(default_factory=dict)
    edits: list[Edit] = field(default_factory=list)
    unterminated: bool = False
    errors: list[str] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)
    raw: str = ""


def _strip_fence(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 2 and _FENCE.match(lines[0]) and lines[-1].strip() == "```":
        return lines[1:-1]
    return lines


def parse(text: str) -> Parsed:
    out = Parsed()
    kind, arg, buf = None, "", []

    def close():
        nonlocal kind, buf
        body = _strip_fence(buf)
        if kind == "NOTE":
            out.note = "\n".join(body).strip()
        elif kind == "JSON":
            out.json_text = "\n".join(body).strip()
        elif kind == "FILE" and arg:
            out.files[arg] = "\n".join(body) + "\n"
        elif kind == "EDIT" and arg:
            find, replace, target = [], [], None
            for line in buf:
                if _FIND.match(line):
                    target = find
                elif _REPLACE.match(line):
                    target = replace
                elif target is not None:
                    target.append(line)
            if "".join(find).strip() and target is replace:
                out.edits.append(Edit(arg, "\n".join(find), "\n".join(replace)))
            else:
                out.errors.append(f"EDIT {arg}: blokda <<<FIND>>> yoki <<<REPLACE>>> belgisi yo'q")
                out.failed_paths.append(arg)
        elif kind in ("FILE", "EDIT"):
            out.errors.append(f"{kind} blokida fayl yo'li ko'rsatilmagan")
        kind, buf = None, []

    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            if kind:
                close()
            kind, arg = header.group(1), (header.group(2) or "").strip()
        elif _END.match(line):
            if kind:
                close()
        elif kind:
            buf.append(line)
    if kind:
        out.unterminated = kind in ("FILE", "EDIT", "JSON")
        close()
    return out


def parse_json(text: str):
    """JSON from a <<<JSON>>> block, tolerating fences and surrounding prose."""
    candidates = [parse(text).json_text, text]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        start = min((i for i in (candidate.find("{"), candidate.find("[")) if i >= 0), default=-1)
        end = max(candidate.rfind("}"), candidate.rfind("]"))
        if 0 <= start < end:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                pass
    return None


def apply_edit(content: str, edit: Edit) -> str | None:
    """Apply one find/replace. Falls back to matching with trailing whitespace ignored."""
    find = edit.find.strip("\n")
    replace = edit.replace.strip("\n")
    if find and content.count(find) >= 1:
        return content.replace(find, replace, 1)
    want = [l.rstrip() for l in find.split("\n")]
    lines = content.split("\n")
    stripped = [l.rstrip() for l in lines]
    for i in range(len(lines) - len(want) + 1):
        if stripped[i : i + len(want)] == want:
            return "\n".join(lines[:i] + replace.split("\n") + lines[i + len(want) :])
    want = [l.strip() for l in want if l.strip()]
    if want:
        for i in range(len(lines)):
            j, k = i, 0
            while j < len(lines) and k < len(want):
                if not lines[j].strip():
                    j += 1
                elif lines[j].strip() == want[k]:
                    j, k = j + 1, k + 1
                else:
                    break
            if k == len(want):
                return "\n".join(lines[:i] + replace.split("\n") + lines[j:])
    return None
