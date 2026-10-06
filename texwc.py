#!/usr/bin/env python3
"""Count words in a LaTeX document the way LibreOffice Writer / MS Word count the .docx.

  python3 texwc.py count                 total + per-section / per-kind breakdown
  python3 texwc.py count --docx X.docx   count a .docx with the same rules
  python3 texwc.py diff                  token-level diff tex vs reference docx
  python3 texwc.py calibrate             compare all available counts
  python3 texwc.py probe "a – b" "x / y" LibreOffice live count vs our tokenizer
  python3 texwc.py setup                 check the installation ([system] in the config)
  python3 texwc.py trace --out DIR       write the output of every pipeline stage to DIR

All rules are in wordcount.toml (--config to use another file).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from wordcount.common import Block, Config, Tokenizer, apply_filters, load_config
from wordcount.docx_extract import DocxExtractor, stored_word_count
from wordcount.report import breakdown, print_diff, print_kinds, print_sections, total
from wordcount.system import resolve, utf8_env

HERE = Path(__file__).resolve().parent
LO_COUNT = HERE / "wordcount" / "lo_count.py"


# --------------------------------------------------------------------------- helpers

def tex_blocks(cfg: Config, verbose: bool) -> list[Block]:
    from wordcount.tex_extract import extract_tex
    blocks, warnings, pre = extract_tex(cfg)
    if verbose:
        print_warnings(warnings)
    return blocks


def print_warnings(warnings: list[str]):
    """Summarise what pandoc skipped, so unhandled macros are easy to spot."""
    skipped = Counter()
    other = []
    for w in warnings:
        m = re.search(r"Skipped '(\\[A-Za-z@]+\*?)(?:\{([^}]*)\})?", w)
        if m:
            name = m.group(1) + (f"{{{m.group(2)}}}" if m.group(1) in ("\\begin", "\\end") else "")
            if not re.match(r"\\(begin|end)\{wc", name):
                skipped[name] += 1
        else:
            other.append(w)
    if skipped:
        print("pandoc skipped (no text produced):", ", ".join(f"{k}×{v}" for k, v in skipped.most_common()),
              file=sys.stderr)
    for w in other:
        print("warning:", w, file=sys.stderr)


def run_lo_count(cfg: Config, args: list[str]) -> str:
    """Run lo_count.py with LibreOffice's Python; returns its output."""
    system = resolve(cfg)
    if not system.lo_python or not system.soffice:
        raise RuntimeError("LibreOffice or its Python not found (run `texwc.py setup`)")
    return subprocess.run([system.lo_python, str(LO_COUNT), "--soffice", system.soffice] + args,
                          capture_output=True, text=True, encoding="utf-8", env=utf8_env(),
                          timeout=300, check=True).stdout


def lo_live_count(cfg: Config, path: Path) -> int | None:
    try:
        return int(run_lo_count(cfg, [str(path)]).split()[0])
    except Exception as e:  # LibreOffice / uno missing
        print(f"(LibreOffice live count unavailable: {e})", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- commands

def cmd_count(cfg: Config, args):
    tok = Tokenizer(cfg, args.mode)
    if args.docx:
        blocks = DocxExtractor(cfg).extract(args.docx)
        source = args.docx
    else:
        blocks = tex_blocks(cfg, args.verbose)
        source = cfg["input"]["tex"]
    blocks = apply_filters(blocks, cfg, tok)
    by_section, by_kind = breakdown(blocks, args.depth)

    if args.dump_tokens:
        with open(args.dump_tokens, "w", encoding="utf-8") as f:
            for b in blocks:
                f.write(f"## [{b.kind}] {' > '.join(b.section)}\n{' '.join(b.tokens)}\n")
    if args.sections:
        print_sections(by_section)
    if args.kinds:
        print_kinds(by_kind)
    print(f"{total(blocks)} words  ({source}, tokenizer: {tok.mode})")


def cmd_diff(cfg: Config, args):
    tok = Tokenizer(cfg, args.mode)
    tex = apply_filters(tex_blocks(cfg, args.verbose), cfg, tok)
    docx_path = args.docx or cfg.path("reference_docx")
    docx = apply_filters(DocxExtractor(cfg).extract(docx_path), cfg, tok)
    print_diff(tex, docx, args.depth, args.context, args.all)


def cmd_calibrate(cfg: Config, args):
    docx_path = args.docx or cfg.path("reference_docx")
    rows = []
    stored = stored_word_count(docx_path)
    rows.append(("docx: stored in docProps/app.xml", stored))
    if not args.no_lo:
        rows.append(("docx: LibreOffice live count", lo_live_count(cfg, docx_path)))
    for mode in cfg["tokenizer"]:
        if mode == "mode":
            continue
        tok = Tokenizer(cfg, mode)
        rows.append((f"docx: ours, tokenizer={mode}",
                     total(apply_filters(DocxExtractor(cfg).extract(docx_path), cfg, tok))))
        rows.append((f"tex : ours, tokenizer={mode}",
                     total(apply_filters(tex_blocks(cfg, False), cfg, tok))))
    ref = cfg["input"].get("word_reference_count")
    if ref:
        rows.append(("docx: MS Word (from config)", ref))
    for label, n in rows:
        print(f"  {label:<40} {n if n is not None else '-':>6}")


def cmd_probe(cfg: Config, args):
    tok = Tokenizer(cfg, args.mode)
    try:
        out = run_lo_count(cfg, sum((["--text", t] for t in args.text), [])).splitlines()
        lo = [int(line.split("\t")[0]) for line in out]
    except Exception as e:
        print(f"(LibreOffice unavailable: {e})", file=sys.stderr)
        lo = [None] * len(args.text)
    print(f"{'LO':>4} {'ours':>4}  text → our tokens")
    for t, n in zip(args.text, lo):
        toks = tok(t)
        flag = "" if n == len(toks) else "   <-- mismatch"
        print(f"{n if n is not None else '-':>4} {len(toks):>4}  {t!r} → {toks}{flag}")


def cmd_trace(cfg: Config, args):
    from wordcount.trace import write_trace
    docx = Path(args.docx) if args.docx else None
    write_trace(cfg, Tokenizer(cfg, args.mode), Path(args.out), args.depth, docx, args.context)


def cmd_setup(cfg: Config, args):
    system = resolve(cfg)
    ok = True

    def check(label: str, good: bool, detail: str, hint: str = "", required: bool = True):
        nonlocal ok
        status = "OK" if good else ("MISSING" if required else "optional, missing")
        ok &= good or not required
        print(f"  {label:<18} {status:<18} {detail}")
        if not good and hint:
            print(f"  {'':<18} -> {hint}")

    print(f"Platform: {system.platform} ([system] platform = {cfg.get('system', {}).get('platform', 'auto')!r})")
    v = sys.version_info
    check("Python", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}", "Python 3.11 or newer is needed (tomllib)")

    pandoc_version = ""
    if system.pandoc:
        out = subprocess.run([system.pandoc, "--version"], capture_output=True, text=True,
                             encoding="utf-8").stdout
        pandoc_version = out.split("\n")[0]
    major = int(m.group(1)) if (m := re.search(r"(\d+)\.", pandoc_version)) else 0
    check("pandoc", major >= 3, f"{system.pandoc or '-'}  {pandoc_version}",
          "install pandoc >= 3 (https://pandoc.org/installing.html) or set [system] pandoc")

    from wordcount.tex_extract import bib_paths
    for key in ("tex", "csl"):
        path = cfg.path(key)
        check(f"[input] {key}", bool(path and path.is_file()), str(path), f"check [input] {key} or use --{key}")
    bibs = bib_paths(cfg)
    source = "" if cfg["input"].get("bib") else "  (from \\addbibresource in the tex)"
    check("[input] bib", bool(bibs) and all(b.is_file() for b in bibs),
          ", ".join(map(str, bibs)) + source or "-", "set [input] bib or use --bib")

    # LibreOffice: only for calibrate / probe
    hint = "install LibreOffice, or set [system] soffice / lo_python (only needed for calibrate and probe)"
    check("soffice", bool(system.soffice), system.soffice or "-", hint, required=False)
    uno = False
    if system.lo_python:
        uno = subprocess.run([system.lo_python, "-c", "import uno"], capture_output=True,
                             env=utf8_env()).returncode == 0
    check("LibreOffice Python", uno, f"{system.lo_python or '-'}  (import uno)", hint, required=False)

    print("Ready." if ok else "Not ready: fix the MISSING items above.")
    return 0 if ok else 1


def main(argv=None):
    # UTF-8 output everywhere (Windows consoles / redirected output default to a legacy codepage)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "wordcount.toml"))
    ap.add_argument("--mode", help="tokenizer mode (overrides [tokenizer] mode)")
    ap.add_argument("--tex", help="LaTeX file (overrides [input] tex)")
    ap.add_argument("--bib", help="bibliography file (overrides [input] bib)")
    ap.add_argument("--csl", help="citation style file (overrides [input] csl)")
    ap.add_argument("--until", metavar="HEADING",
                    help="count only text before this heading (regex, overrides [count] stop_at_heading)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("count", help="count words")
    p.add_argument("--docx", help="count this .docx instead of the tex")
    p.add_argument("-s", "--sections", action="store_true", help="per-section breakdown")
    p.add_argument("-k", "--kinds", action="store_true", help="per-kind breakdown")
    p.add_argument("--depth", type=int, default=2, help="section depth for the breakdown")
    p.add_argument("--dump-tokens", metavar="FILE", help="write every counted token, per block")
    p.add_argument("-v", "--verbose", action="store_true", help="show pandoc/preprocessor warnings")

    p = sub.add_parser("diff", help="tex vs reference docx")
    p.add_argument("--docx")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--context", type=int, default=6)
    p.add_argument("--all", action="store_true", help="also list matching sections/kinds")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("calibrate", help="compare all available counts")
    p.add_argument("--docx")
    p.add_argument("--no-lo", action="store_true", help="skip the LibreOffice live count")

    p = sub.add_parser("probe", help="LibreOffice live count vs our tokenizer for snippets")
    p.add_argument("text", nargs="+")

    p = sub.add_parser("trace", help="write the output of every pipeline stage to a folder")
    p.add_argument("--out", default="trace", help="output folder (default: trace)")
    p.add_argument("--docx", help="also trace this .docx and diff it against the tex")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--context", type=int, default=6)

    sub.add_parser("setup", help="check that pandoc, LibreOffice and the input files are found")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    # paths given on the command line are relative to the current directory
    for key in ("tex", "bib", "csl"):
        if getattr(args, key):
            cfg["input"][key] = str(Path(getattr(args, key)).resolve())
    if args.until is not None:
        cfg["count"]["stop_at_heading"] = args.until
    commands = {"count": cmd_count, "diff": cmd_diff, "calibrate": cmd_calibrate,
                "probe": cmd_probe, "setup": cmd_setup, "trace": cmd_trace}
    sys.exit(commands[args.cmd](cfg, args))


if __name__ == "__main__":
    main()
