"""LaTeX -> kind-tagged text blocks, via the preprocessor and pandoc (with citeproc)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .common import FRONT_MATTER, HEADERS, Block, Config
from .system import resolve
from .tex_preprocess import TexPreprocessor, find_bib_files

KIND_DIVS = {"wcguidance": "guidance", "wctable": "table", "wctoc": "toc",
             "wcbibliography": "bibliography", "wcheader": "header", "wccaption": "caption"}


def bib_paths(cfg: Config) -> list[Path]:
    """[input] bib if set, otherwise the files named by \\addbibresource / \\bibliography in the tex."""
    bib = cfg.path("bib")
    if bib:
        return [bib]
    tex = cfg.path("tex")
    return find_bib_files(tex) if tex and tex.is_file() else []


def run_pandoc(tex: str, cfg: Config, with_bibliography: bool) -> tuple[dict, list[str]]:
    pandoc = resolve(cfg).pandoc
    if not pandoc:
        raise SystemExit("pandoc not found: install it or set [system] pandoc in the config "
                         "(run `texwc.py setup` to check)")
    cmd = [pandoc, "-f", "latex", "-t", "json", "--verbose",
           "--metadata", "link-citations=false"]
    bibs, csl = bib_paths(cfg), cfg.path("csl")
    for path in bibs + ([csl] if csl else []):
        if not path.is_file():
            raise SystemExit(f"file not found: {path}\n(set it with --bib / --csl or under [input] in the config)")
    if bibs:
        cmd += ["--citeproc"]
        for bib in bibs:
            cmd += ["--bibliography", str(bib)]
        if csl:
            cmd += ["--csl", str(csl)]
        if locale := cfg["input"].get("locale"):
            cmd += ["--metadata", f"lang={locale}"]
        if not with_bibliography:
            cmd += ["--metadata", "suppress-bibliography=true"]
    proc = subprocess.run(cmd, input=tex, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode:
        errors = [line for line in proc.stderr.splitlines() if not line.startswith("[INFO]")]
        raise SystemExit("pandoc failed:\n" + "\n".join(errors[-15:]))
    notes = [line for line in proc.stderr.splitlines()
             if re.search(r"Skipped|Could not|not found|WARNING", line, re.I)]
    return json.loads(proc.stdout), notes


class AstWalker:
    def __init__(self, cfg: Config):
        self.math = cfg["tex"].get("math", "one")
        self.blocks: list[Block] = []
        self.section: list[str] = []
        self.refs: list = []
        self.raw: set[str] = set()

    # ---- inlines
    def text(self, inlines: list) -> str:
        out = []
        for el in inlines:
            t, c = el["t"], el.get("c")
            if t == "Str":
                out.append(c)
            elif t in ("Space", "SoftBreak", "LineBreak"):
                out.append(" ")
            elif t in ("Emph", "Strong", "Underline", "Strikeout", "Superscript", "Subscript", "SmallCaps"):
                out.append(self.text(c))
            elif t == "Quoted":
                q = "“”" if c[0]["t"] == "DoubleQuote" else "‘’"
                out.append(q[0] + self.text(c[1]) + q[1])
            elif t == "Cite":
                out.append(self.text(c[1]))
            elif t in ("Span", "Link"):
                out.append(self.text(c[1]))
            elif t == "Code":
                out.append(c[1])
            elif t == "Math":
                out.append({"one": "MATH", "skip": "", "tex": c[1]}[self.math])
            elif t == "Note":
                self._pending_notes.append(c)
            elif t == "RawInline":
                self.raw.add(c[1][:40])
            # Image: alt text is not counted
        return "".join(out)

    # ---- blocks
    def emit(self, kind: str, text: str):
        self.blocks.append(Block(kind, text, tuple(self.section) or (FRONT_MATTER,)))

    def walk(self, blocks: list, kind: str = "para"):
        for b in blocks:
            t, c = b["t"], b.get("c")
            self._pending_notes = []
            if t in ("Para", "Plain"):
                self.emit(kind, self.text(c))
            elif t == "Header":
                title = self.text(c[2])
                level = c[0]
                self.section = self.section[: level - 1] + [title.strip()]
                self.emit("heading", title)
            elif t == "Div":
                (ident, classes, _), content = c
                k = next((KIND_DIVS[cl] for cl in classes if cl in KIND_DIVS), kind)
                if k == "header":
                    saved, self.section = self.section, [HEADERS]
                    self.walk(content, k)
                    self.section = saved
                elif "wcbibliography" in classes:
                    self.walk(self.refs, "bibliography")
                else:
                    self.walk(content, k)
            elif t == "Figure":
                _, (short, caption), content = c
                self.walk(content, kind)
                self.walk(caption, "caption")
            elif t == "Table":
                self._table(c)
            elif t in ("BulletList", "OrderedList"):
                items = c if t == "BulletList" else c[1]
                for item in items:
                    self.walk(item, kind)
            elif t == "DefinitionList":
                for term, defs in c:
                    self.emit(kind, self.text(term))
                    for d in defs:
                        self.walk(d, kind)
            elif t == "BlockQuote":
                self.walk(c, kind)
            elif t == "LineBlock":
                for line in c:
                    self.emit(kind, self.text(line))
            elif t == "CodeBlock":
                self.emit(kind, c[1])
            elif t == "RawBlock":
                self.raw.add(c[1][:40])
            for note in self._pending_notes:
                self.walk(note, "footnote")

    def _table(self, c):
        _, caption, _, head, bodies, foot = c
        rows = list(head[1])
        for body in bodies:
            rows += body[2] + body[3]
        rows += foot[1]
        for row in rows:
            for cell in row[1]:
                self.walk(cell[4], "table")
        self.walk(caption[1], "caption")


def extract_tex(cfg: Config) -> tuple[list[Block], list[str], str]:
    """Return (blocks, warnings, preprocessed LaTeX)."""
    pre = TexPreprocessor(cfg)
    tex = pre.run(cfg.path("tex"))
    render_bib = pre.has_bibliography and cfg["tex"]["bibliography"].get("mode", "render") == "render"
    ast, notes = run_pandoc(tex, cfg, render_bib)

    walker = AstWalker(cfg)
    blocks = ast["blocks"]
    # citeproc appends the reference list as Div#refs; it is moved to \printbibliography
    walker.refs = [b for b in blocks if b["t"] == "Div" and b["c"][0][0] == "refs"]
    blocks = [b for b in blocks if not (b["t"] == "Div" and b["c"][0][0] == "refs")]
    walker.walk(blocks)

    for rule in cfg["tex"].get("replacements", []):
        pat = re.compile(rule["pattern"])
        for b in walker.blocks:
            if b.kind in rule.get("kinds", [b.kind]):
                b.text = pat.sub(rule["replace"], b.text)

    warnings = pre.warnings + notes + [f"raw LaTeX left after pandoc: {r!r}" for r in sorted(walker.raw)]
    return walker.blocks, warnings, tex
