"""Write the intermediate result of every pipeline stage to a folder (`texwc.py trace`).

The files are numbered like the stages in the README diagram:

  0_input.tex          the document body as read (\\input inlined, comments stripped)
  1_preprocess.diff    what each tex_preprocess step changed
  1_preprocessed.tex   the LaTeX that pandoc reads
  2_pandoc.txt         the pandoc command and its warnings
  2_pandoc_ast.json    pandoc's output (citations already rendered by citeproc)
  3_blocks.tsv         tex_extract: the text blocks with their kind and section
  4_filter.tsv         filter: every block, kept or dropped, and why
  5_tokens.txt         tokenize: the words of every kept block
  6_count.txt          the word count, per section and per kind

With a .docx, 3-6 are also written for it (3_docx_blocks.tsv ...) plus 7_diff.txt.
"""
from __future__ import annotations

import difflib
import io
import json
import os
import re
from pathlib import Path

from .common import Block, Config, Tokenizer, apply_filters
from .report import breakdown, print_diff, print_kinds, print_sections, total
from .tex_extract import extract_tex

# preprocessor steps in the order TexPreprocessor.run applies them
STEPS = {
    "drop_environments": "drop [tex] drop_environments (with their content)",
    "macros": "expand [tex.macros]",
    "tables": "flatten [tex.tables] into one paragraph per cell (wctable)",
    "structure": "number headings and captions, resolve \\ref, build the TOC, place the bibliography",
    "headers_footers": "append fancyhdr header/footer text ([tex.headers], wcheader)",
}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _rel(arg: str) -> str:
    """Paths under the current directory are shown relative, so the trace does not contain home paths."""
    try:
        if os.path.isabs(arg) and Path(arg).resolve().is_relative_to(Path.cwd()):
            return os.path.relpath(arg)
    except (OSError, ValueError):
        pass
    return arg


def _write(out: Path, name: str, text: str) -> str:
    (out / name).write_text(text, encoding="utf-8")
    return name


def _blocks_tsv(blocks: list[Block], before: list[str] | None = None) -> str:
    if before and all(t == b.text for t, b in zip(before, blocks)):
        before = None  # no replacement changed anything
    lines = ["#\tkind\tsection\ttext" + ("\tbefore [tex.replacements]" if before else "")]
    for i, b in enumerate(blocks):
        row = [str(i), b.kind, " > ".join(b.section), _one_line(b.text)]
        if before:
            row.append(_one_line(before[i]) if before[i] != b.text else "")
        lines.append("\t".join(row))
    return "\n".join(lines) + "\n"


def _filter_tsv(blocks: list[Block], log: list) -> str:
    index = {id(b): i for i, b in enumerate(blocks)}
    lines = ["#\tdecision\treason\twords\tkind\ttext"]
    for b, reason in log:
        keep = reason == "kept"
        lines.append("\t".join([str(index[id(b)]), "KEEP" if keep else "DROP", reason,
                                str(len(b.tokens)) if keep else "", b.kind, _one_line(b.text)[:90]]))
    return "\n".join(lines) + "\n"


def _tokens_txt(blocks: list[Block], kept: list[Block], tok: Tokenizer) -> str:
    index = {id(b): i for i, b in enumerate(blocks)}
    out = [f"# tokenizer: {tok.mode}; every ⟦…⟧ is one counted word\n"]
    running = 0
    for b in kept:
        running += len(b.tokens)
        out.append(f"## #{index[id(b)]} [{b.kind}] {' > '.join(b.section)}"
                   f"   ({len(b.tokens)} word{'s' * (len(b.tokens) != 1)}, running total {running})")
        out.append(" ".join(f"⟦{t}⟧" for t in b.tokens) + "\n")
    return "\n".join(out)


def _count_txt(kept: list[Block], depth: int, source: str, tok: Tokenizer) -> str:
    buf = io.StringIO()
    by_section, by_kind = breakdown(kept, depth)
    print_sections(by_section, buf)
    print_kinds(by_kind, buf)
    print(f"{total(kept)} words  ({source}, tokenizer: {tok.mode})", file=buf)
    return buf.getvalue()


def _count_side(out: Path, prefix: str, blocks: list[Block], cfg: Config, tok: Tokenizer,
                depth: int, source: str, before: list[str] | None = None) -> list[Block]:
    """Stages 3-6 for one side (tex or docx); returns the kept blocks."""
    log: list = []
    kept = apply_filters(blocks, cfg, tok, log)
    f3 = _write(out, f"3_{prefix}blocks.tsv", _blocks_tsv(blocks, before))
    print(f"3 extract     {len(blocks)} blocks: "
          + ", ".join(f"{k} {n}" for k, n in _kinds(blocks).items()) + f"  → {f3}")
    f4 = _write(out, f"4_{prefix}filter.tsv", _filter_tsv(blocks, log))
    print(f"4 filter      kept {len(kept)}, dropped {len(blocks) - len(kept)}  → {f4}")
    f5 = _write(out, f"5_{prefix}tokens.txt", _tokens_txt(blocks, kept, tok))
    print(f"5 tokenize    {total(kept)} tokens  → {f5}")
    f6 = _write(out, f"6_{prefix}count.txt", _count_txt(kept, depth, source, tok))
    print(f"6 count       {total(kept)} words  → {f6}")
    return kept


def _kinds(blocks: list[Block]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b.kind] = counts.get(b.kind, 0) + 1
    return counts


def write_trace(cfg: Config, tok: Tokenizer, out: Path, depth: int = 2,
                docx: Path | None = None, context: int = 6):
    out.mkdir(parents=True, exist_ok=True)
    tex_path = cfg.path("tex")
    print(f"tracing {_rel(str(tex_path))} → {_rel(str(out.resolve()))}/")

    trace: dict = {}
    blocks, warnings, _ = extract_tex(cfg, trace)
    pre = trace["preprocessor"]
    stages = pre.stages

    # 0 input
    f0 = _write(out, "0_input.tex", stages["input"])
    print(f"0 input       {stages['input'].count(chr(10))} lines of document body  → {f0}")

    # 1 tex_preprocess: one diff per step
    diff = [f"% read from the preamble: \\contentsname = {pre.toc_title!r}, tocdepth = {pre.toc_depth}, "
            f"headers = {pre.headers}, footers = {pre.footers}\n"]
    prev, prev_name = stages["input"], "input"
    changed = []
    for step, what in STEPS.items():
        cur = stages[step]
        lines = list(difflib.unified_diff(prev.splitlines(keepends=True), cur.splitlines(keepends=True),
                                          f"{prev_name}", f"{step}", n=1))
        diff.append(f"\n{'=' * 78}\n= step: {step} — {what}\n{'=' * 78}\n")
        diff.append("".join(lines) if lines else "(no change)\n")
        if lines:
            changed.append(step)
        prev, prev_name = cur, step
    f1 = _write(out, "1_preprocess.diff", "".join(diff))
    f1b = _write(out, "1_preprocessed.tex", trace["preprocessed"])
    print(f"1 preprocess  steps that changed the text: {', '.join(changed) or 'none'}  → {f1}, {f1b}")

    # 2 pandoc + citeproc
    cmd = " ".join(_rel(a) if " " not in a else repr(_rel(a)) for a in
                   [Path(trace["pandoc_cmd"][0]).name] + trace["pandoc_cmd"][1:])
    # pandoc reports the \begin{wc...} markers as skipped, but keeps their content as a Div with that class
    warnings = [w for w in warnings if not re.search(r"Skipped '\\(begin|end)\{wc", w)]
    f2 = _write(out, "2_pandoc.txt", f"{cmd} < 1_preprocessed.tex > 2_pandoc_ast.json\n\n"
                + ("warnings (LaTeX that produced no text):\n" + "".join(f"  {w}\n" for w in warnings)
                   if warnings else "no warnings\n"))
    ast = trace["ast"]
    f2b = _write(out, "2_pandoc_ast.json", json.dumps(ast, ensure_ascii=False, indent=1) + "\n")
    print(f"2 pandoc      {len(ast['blocks'])} top-level AST blocks, {len(warnings)} warnings  → {f2}, {f2b}")

    # 3-6 tex side
    tex = _count_side(out, "", blocks, cfg, tok, depth, _rel(str(tex_path)), trace["texts_before_replacements"])

    if docx:
        from .docx_extract import DocxExtractor
        print(f"docx side: {_rel(str(Path(docx).resolve()))}")
        dblocks = DocxExtractor(cfg).extract(docx)
        dkept = _count_side(out, "docx_", dblocks, cfg, tok, depth, _rel(str(Path(docx).resolve())))
        with open(out / "7_diff.txt", "w", encoding="utf-8") as f:
            print_diff(tex, dkept, depth, context, False, f)
        print(f"7 diff        tex {total(tex)} vs docx {total(dkept)} ({total(tex) - total(dkept):+d})  → 7_diff.txt")
