"""Rewrite a LaTeX document into plain LaTeX that pandoc reads like the .docx looks.

Everything a word processor shows as literal text but LaTeX generates is made
explicit here: heading numbers, caption labels, \\ref numbers, the table of
contents, header/footer text. Custom macros are expanded from the config, and
the kind of text (guidance box, table cell, TOC, bibliography) is marked with
`wc<kind>` environments, which pandoc turns into Divs.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .common import Config

# --------------------------------------------------------------------------- low-level parsing


def read_group(s: str, i: int) -> tuple[str, int]:
    """s[i] == '{': return (content, index after the closing brace)."""
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    raise ValueError(f"unbalanced braces from position {i}: {s[i:i + 60]!r}")


def read_optional(s: str, i: int) -> tuple[str | None, int]:
    """Read an optional [..] argument at s[i] (no whitespace skipped)."""
    if i >= len(s) or s[i] != "[":
        return None, i
    depth = 0
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth == 0:
            return s[i + 1 : j], j + 1
        j += 1
    return None, i


def skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    return i


def read_arg(s: str, i: int) -> tuple[str, int]:
    """Read one mandatory argument: a {group}, a control sequence or a single character."""
    i = skip_ws(s, i)
    if i >= len(s):
        return "", i
    if s[i] == "{":
        return read_group(s, i)
    if s[i] == "\\":
        m = re.match(r"\\([A-Za-z@]+|.)", s[i:])
        return m.group(0), i + m.end()
    return s[i], i + 1


def read_args(s: str, i: int, n: int, star: bool = True) -> tuple[list[str], str | None, int]:
    """After a command name: optional '*', optional [..], then n mandatory args."""
    if star and i < len(s) and s[i] == "*":
        i += 1
    opt, i = read_optional(s, skip_ws(s, i) if n else i)
    args = []
    for _ in range(n):
        a, i = read_arg(s, i)
        args.append(a)
    return args, opt, i


def find_command(s: str, names: list[str], start: int = 0):
    """Find the next \\name (not followed by a letter) for any of names."""
    if not names:
        return None
    pat = re.compile(r"\\(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![A-Za-z@])")
    return pat.search(s, start)


def find_env_end(s: str, env: str, start: int) -> int:
    """Index of the matching \\end{env} (handles nesting)."""
    pat = re.compile(r"\\(begin|end)\s*\{" + re.escape(env) + r"\}")
    depth = 1
    for m in pat.finditer(s, start):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start()
    raise ValueError(f"no \\end{{{env}}}")


def strip_comments(s: str) -> str:
    out = []
    for line in s.split("\n"):
        # % starts a comment unless escaped by an odd number of backslashes
        m = re.match(r".*?(?<!\\)(?:\\\\)*(?=%)", line)
        if m:
            out.append(line[: m.end()] + "\x00")  # \x00: the newline is eaten
        else:
            out.append(line + "\n")
    return "".join(out).replace("\x00", "")


# --------------------------------------------------------------------------- preprocessing


def find_bib_files(tex_path: Path) -> list[Path]:
    """Bibliography files named by \\addbibresource{x.bib} / \\bibliography{a,b}, relative to the tex."""
    src = strip_comments(tex_path.read_text(encoding="utf-8"))
    files = []
    for m in re.finditer(r"\\addbibresource\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", src):
        files.append(m.group(1).strip())
    for m in re.finditer(r"\\bibliography\s*\{([^}]*)\}", src):
        files += [f.strip() if f.strip().endswith(".bib") else f.strip() + ".bib" for f in m.group(1).split(",")]
    return [tex_path.parent / f for f in files if f]


def format_date(d: dt.date, fmt: str) -> str:
    """strftime that also understands %-d / %-m (no leading zero) on Windows."""
    fmt = fmt.replace("%-d", str(d.day)).replace("%-m", str(d.month))
    return d.strftime(fmt)


def to_letters(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class TexPreprocessor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.t = cfg["tex"]
        self.warnings: list[str] = []
        self.has_bibliography = False
        self.stages: dict[str, str] = {}   # body after each step, for `texwc.py trace`

    # ---- entry point
    def run(self, path: Path) -> str:
        src = self._read(path)
        src = strip_comments(src)
        m_begin = re.search(r"\\begin\s*\{document\}", src)
        m_end = re.search(r"\\end\s*\{document\}", src)
        if not m_begin:
            raise ValueError("no \\begin{document}")
        preamble = src[: m_begin.start()]
        body = src[m_begin.end() : m_end.start() if m_end else len(src)]

        self._read_preamble(preamble)
        self.stages["input"] = body
        for env in self.t.get("drop_environments", []):
            body = self._drop_env(body, env)
        self.stages["drop_environments"] = body
        body = self._expand_macros(body)
        self.stages["macros"] = body
        body = self._tables(body)
        self.stages["tables"] = body
        body = self._structure(body)
        self.stages["structure"] = body
        body += self._headers_footers()
        self.stages["headers_footers"] = body
        return "\\begin{document}\n" + body + "\n\\end{document}\n"

    def _read(self, path: Path) -> str:
        src = path.read_text(encoding="utf-8")
        if not self.t.get("follow_inputs", True):
            return src

        def repl(m):
            name = m.group(2).strip()
            p = (path.parent / name)
            if not p.suffix:
                p = p.with_suffix(".tex")
            if not p.exists():
                self.warnings.append(f"\\{m.group(1)}{{{name}}}: file not found")
                return ""
            return self._read(p)

        return re.sub(r"\\(input|include)\s*\{([^}]*)\}", repl, src)

    # ---- preamble: things that affect visible text
    def _read_preamble(self, pre: str):
        toc = self.t["toc"]
        m = re.search(r"\\renewcommand\s*\{?\\contentsname\}?\s*\{([^}]*)\}", pre)
        self.toc_title = toc.get("title") or (m.group(1) if m else "Contents")
        m = re.search(r"\\setcounter\s*\{tocdepth\}\s*\{(\d+)\}", pre)
        self.toc_depth = int(m.group(1)) if m else toc.get("depth", 3)

        # fancyhdr: \fancyhead[L]{..}, \fancyfoot[R]{..}, \lhead{..} ...
        self.headers: list[str] = []
        self.footers: list[str] = []
        for m in re.finditer(r"\\(fancyhead|fancyfoot|[lcr]head|[lcr]foot)(?![A-Za-z])", pre):
            args, _, _ = read_args(pre, m.end(), 1, star=False)
            text = args[0]
            if not text.strip():
                continue
            (self.footers if "foot" in m.group(1) else self.headers).append(text)

    def _headers_footers(self) -> str:
        h = self.t.get("headers", {})
        if not h.get("include", True):
            return ""
        copies = h.get("copies", 1)
        page = h.get("page", "0")
        out = []
        for text in self.headers * copies + self.footers * copies:
            text = re.sub(r"\\thepage(?![A-Za-z])", page, text)
            out.append(f"\\begin{{wcheader}}\n{text}\n\\end{{wcheader}}\n")
        return "\n" + "".join(out)

    # ---- macros
    def _macro_table(self) -> dict[str, tuple[int, str]]:
        table = {}
        for name, spec in self.t.get("macros", {}).items():
            if isinstance(spec, dict):
                tmpl = spec.get("template", "")
                n = spec.get("args", max((int(k) for k in re.findall(r"#(\d)", tmpl)), default=0))
            else:
                tmpl = spec
                n = max((int(k) for k in re.findall(r"#(\d)", tmpl)), default=0)
            table[name] = (n, tmpl)
        return table

    def _expand_macros(self, s: str) -> str:
        table = self._macro_table()
        names = list(table)
        for _ in range(50):
            m = find_command(s, names)
            if not m:
                return s
            changed = False
            out = []
            pos = 0
            while m:
                name = m.group(1)
                n, tmpl = table[name]
                i = m.end()
                if n:
                    args, _, i = read_args(s, i, n)
                else:
                    if i < len(s) and s[i] == "*":
                        i += 1
                    i_ws = i
                    while i_ws < len(s) and s[i_ws] in " \t":  # TeX eats spaces after a control word
                        i_ws += 1
                    i = i_ws
                    args = []
                text = tmpl
                for k, a in enumerate(args, 1):
                    text = text.replace(f"#{k}", a)
                out.append(s[pos : m.start()])
                out.append(text)
                pos = i
                changed = True
                m = find_command(s, names, i)
            out.append(s[pos:])
            s = "".join(out)
            if not changed:
                return s
        self.warnings.append("macro expansion did not terminate after 50 passes (recursive macro?)")
        return s

    def _drop_env(self, s: str, env: str) -> str:
        pat = re.compile(r"\\begin\s*\{" + re.escape(env) + r"\}")
        while (m := pat.search(s)):
            end = find_env_end(s, env, m.end())
            close = s.index("}", end) + 1
            s = s[: m.start()] + s[close:]
        return s

    # ---- tables: one paragraph per non-empty cell
    def _tables(self, s: str) -> str:
        envs = {k: v for k, v in self.t.get("tables", {}).items() if isinstance(v, int)}
        rules = self.t.get("tables", {}).get("table_rules", [])
        pat = re.compile(r"\\begin\s*\{(" + "|".join(re.escape(e) for e in envs) + r")\}")
        pos = 0
        while (m := pat.search(s, pos)):
            env = m.group(1)
            # outer options [..], then the mandatory spec arguments
            _, i = read_optional(s, skip_ws(s, m.end()))
            for _ in range(envs[env]):
                _, i = read_arg(s, i)
            # tblr-style inner [..] options after the spec are not used; longtblr has [outer]{inner}
            end = find_env_end(s, env, i)
            close = s.index("}", end) + 1
            cells = self._split_table(s[i:end], rules)
            repl = "\\begin{wctable}\n" + "".join(f"{c}\n\n" for c in cells) + "\\end{wctable}\n"
            s = s[: m.start()] + repl + s[close:]
            pos = m.start() + len(repl)
        return s

    def _split_table(self, body: str, rules: list[str]) -> list[str]:
        # remove rule commands with their arguments
        for r in rules:
            while (m := find_command(body, [r])):
                _, _, j = read_args(body, m.end(), 1 if r in ("cline", "cmidrule", "SetCell", "SetRow", "SetHline", "SetVline", "hhline") else 0)
                body = body[: m.start()] + body[j:]
        # \multicolumn{n}{spec}{text}, \multirow{n}{w}{text} -> text
        for cmd in ("multicolumn", "multirow", "SetCell"):
            while (m := find_command(body, [cmd])):
                args, _, j = read_args(body, m.end(), 3 if cmd != "SetCell" else 1)
                body = body[: m.start()] + (args[-1] if cmd != "SetCell" else "") + body[j:]
        # split on top-level \\ and &
        cells, cur, depth, i = [], [], 0, 0
        while i < len(body):
            c = body[i]
            if c == "\\":
                if body.startswith("\\\\", i):
                    cells.append("".join(cur))
                    cur = []
                    i += 2
                    _, i = read_optional(body, skip_ws(body, i))  # \\[2pt]
                    continue
                cur.append(body[i : i + 2])
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            if c == "&" and depth == 0:
                cells.append("".join(cur))
                cur = []
            else:
                cur.append(c)
            i += 1
        cells.append("".join(cur))
        return [c.strip() for c in cells if c.strip()]

    # ---- headings, captions, labels, refs, TOC, bibliography, \today
    def _structure(self, s: str) -> str:
        levels = self.t["heading_levels"]
        fmts = self.t["heading_format"]
        cap = self.t["captions"]
        refs = self.t["refs"]
        commands = levels + ["appendix", "addcontentsline", "caption", "captionof", "label",
                             "tableofcontents", "printbibliography", "today"]
        env_pat = re.compile(r"\\(begin|end)\s*\{(figure\*?|table\*?|appendices)\}")

        counters = [0] * len(levels)
        fig = tab = 0
        appendix = False
        current_label = ""          # number the next \label refers to
        current_label_kind = "section"
        labels: dict[str, tuple[str, str]] = {}
        toc: list[tuple[int, str]] = []
        float_stack: list[str] = []
        out: list[str] = []
        pos = 0

        cmd_pat = re.compile(r"\\(" + "|".join(sorted(commands, key=len, reverse=True)) + r")(?![A-Za-z@])")
        while True:
            mc = cmd_pat.search(s, pos)
            me = env_pat.search(s, pos)
            if me and (not mc or me.start() < mc.start()):
                out.append(s[pos : me.end()] if me.group(2) != "appendices" else s[pos : me.start()])
                pos = me.end()
                if me.group(2) == "appendices":
                    appendix = me.group(1) == "begin" or appendix
                    if me.group(1) == "begin":
                        counters = [0] * len(levels)
                    continue
                if me.group(1) == "begin":
                    float_stack.append(me.group(2).rstrip("*"))
                elif float_stack:
                    float_stack.pop()
                continue
            if not mc:
                break
            name = mc.group(1)
            out.append(s[pos : mc.start()])
            i = mc.end()

            if name in levels:
                lvl = levels.index(name)
                star = i < len(s) and s[i] == "*"
                args, short, i = read_args(s, i, 1)
                title = args[0]
                if star:
                    out.append(f"\\{name}*{{{title}}}")
                else:
                    counters[lvl] += 1
                    for k in range(lvl + 1, len(counters)):
                        counters[k] = 0
                    nums = [str(c) for c in counters[: lvl + 1]]
                    fmt = fmts[lvl]
                    top = self.t.get("appendix_level", 0)
                    if appendix and self.t.get("appendix_letters", True) and lvl >= top:
                        nums = nums[top:]
                        nums[0] = to_letters(counters[top])
                        fmt = self.t.get("appendix_heading_format", fmts)[lvl]
                    num = ".".join(nums)
                    text = fmt.format(n=num, title=title)
                    out.append(f"\\{name}*{{{text}}}")
                    # like main.tex: the lettered appendix headings are \autoref'd as "appendix"
                    is_appendix = appendix and self.t.get("appendix_letters", True) and lvl == top
                    current_label, current_label_kind = num, "appendix" if is_appendix else name
                    if lvl < self.toc_depth:
                        toc.append((lvl, fmt.format(n=num, title=short or title)))
            elif name == "appendix":
                appendix = True
                counters = [0] * len(levels)
            elif name == "addcontentsline":
                args, _, i = read_args(s, i, 3, star=False)
                if args[0].strip() == "toc" and args[1].strip() in levels:
                    lvl = levels.index(args[1].strip())
                    if lvl < self.toc_depth:
                        toc.append((lvl, args[2]))
            elif name in ("caption", "captionof"):
                if name == "captionof":
                    (ftype,), _, i = read_args(s, i, 1, star=False)
                    ftype = ftype.strip()
                else:
                    ftype = float_stack[-1] if float_stack else "figure"
                star = i < len(s) and s[i] == "*"
                args, _, i = read_args(s, i, 1)
                if star:
                    out.append(f"\n\\begin{{wccaption}}\n{args[0]}\n\\end{{wccaption}}\n")
                else:
                    if ftype == "table":
                        tab += 1
                        n = tab
                    else:
                        fig += 1
                        n = fig
                    fmt = cap.get(ftype, cap["figure"])
                    # an explicit block: pandoc drops captions of floats without a real table/image
                    out.append(f"\n\\begin{{wccaption}}\n{fmt.format(n=n, caption=args[0])}\n\\end{{wccaption}}\n")
                    current_label, current_label_kind = str(n), ftype
            elif name == "label":
                args, _, i = read_args(s, i, 1, star=False)
                labels[args[0].strip()] = (current_label, current_label_kind)
            elif name == "tableofcontents":
                if self.t["toc"].get("generate", True):
                    out.append("\\begin{wctoc}\n\x01TOC\x01\n\\end{wctoc}\n")
            elif name == "printbibliography":
                _, opt, i = read_args(s, i, 0)
                self.has_bibliography = True
                if self.t["bibliography"].get("mode", "render") == "render":
                    if not (opt and re.search(r"heading\s*=\s*none", opt)):
                        out.append(f"\\section*{{{self.t['bibliography'].get('title', 'References')}}}\n")
                    out.append("\\begin{wcbibliography}\nWCBIBLIOGRAPHY\n\\end{wcbibliography}\n")
            elif name == "today":
                out.append(format_date(dt.date.today(), self.t.get("date_format", "%B %-d, %Y")))
                if s[i : i + 2] == "{}":
                    i += 2
            pos = i
        out.append(s[pos:])
        s = "".join(out)

        # fill in the TOC now that all headings are known
        entry = self.t["toc"].get("entry_format", "{title} {page}")
        page = self.t["toc"].get("page", "0")
        toc_text = "".join(f"{line}\n\n" for line in
                           [self.toc_title] + [entry.format(title=t, page=page) for _, t in toc])
        s = s.replace("\x01TOC\x01", toc_text)

        # \ref and friends, now that all labels are known
        names = refs.get("autoref_names", {})

        def ref(m):
            cmd, key = m.group(1), m.group(2).strip()
            num, kind = labels.get(key, ("??", ""))
            if key not in labels:
                self.warnings.append(f"\\{cmd}{{{key}}}: unknown label")
            if cmd == "pageref":
                return refs.get("pageref", "0")
            if cmd == "eqref":
                return f"({num})"
            if cmd in ("autoref", "cref", "Cref"):
                return f"{names.get(kind, '')} {num}".strip()
            return refs.get("ref", "{n}").format(n=num)

        s = re.sub(r"\\(ref|eqref|pageref|autoref|cref|Cref)\*?\s*\{([^}]*)\}", ref, s)
        return s

