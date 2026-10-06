"""Count summaries and the tex-vs-docx diff, printed to any stream (shared by count, diff and trace)."""
from __future__ import annotations

import difflib
import sys
from collections import Counter, defaultdict
from typing import TextIO

from .common import Block


def total(blocks: list[Block]) -> int:
    return sum(len(b.tokens) for b in blocks)


def section_key(b: Block, depth: int) -> tuple[str, ...]:
    return b.section[:depth]


def breakdown(blocks: list[Block], depth: int) -> tuple[dict, Counter]:
    by_section: dict[tuple, int] = defaultdict(int)
    by_kind: Counter = Counter()
    for b in blocks:
        by_section[section_key(b, depth)] += len(b.tokens)
        by_kind[b.kind] += len(b.tokens)
    return by_section, by_kind


def print_sections(by_section: dict, out: TextIO = sys.stdout):
    print("Words per section:", file=out)
    for key, n in by_section.items():
        print(f"  {n:6d}  {'  ' * (len(key) - 1)}{key[-1]}", file=out)


def print_kinds(by_kind: Counter, out: TextIO = sys.stdout):
    print("Words per kind:", file=out)
    for k, n in by_kind.most_common():
        print(f"  {n:6d}  {k}", file=out)


def print_diff(tex: list[Block], docx: list[Block], depth: int, context: int, show_all: bool,
               out: TextIO = sys.stdout):
    """Per section / kind comparison, then every differing run of tokens."""
    ts, tk = breakdown(tex, depth)
    ds, dk = breakdown(docx, depth)
    print(f"{'tex':>6} {'docx':>6} {'diff':>6}  section", file=out)
    for key in list(dict.fromkeys(list(ds) + list(ts))):
        a, b = ts.get(key, 0), ds.get(key, 0)
        if a != b or show_all:
            print(f"{a:6d} {b:6d} {a - b:+6d}  {'  ' * (len(key) - 1)}{key[-1]}", file=out)
    print(f"{'tex':>6} {'docx':>6} {'diff':>6}  kind", file=out)
    for k in list(dict.fromkeys(list(dk) + list(tk))):
        a, b = tk.get(k, 0), dk.get(k, 0)
        if a != b or show_all:
            print(f"{a:6d} {b:6d} {a - b:+6d}  {k}", file=out)
    print(f"{total(tex):6d} {total(docx):6d} {total(tex) - total(docx):+6d}  TOTAL\n", file=out)

    # token level
    tt = [(t, b) for b in tex for t in b.tokens]
    dt = [(t, b) for b in docx for t in b.tokens]

    def norm(t: str, b: Block) -> str:
        if b.kind == "toc" and t.isdigit():
            return "#"  # page numbers differ but are one token either way
        return t.replace("...", "…").replace("‑", "-")

    sm = difflib.SequenceMatcher(None, [norm(t, b) for t, b in tt], [norm(t, b) for t, b in dt], autojunk=False)
    hunks = [op for op in sm.get_opcodes() if op[0] != "equal"]
    print(f"{len(hunks)} differing token runs (tex → docx):", file=out)
    for op, a1, a2, b1, b2 in hunks:
        ttoks = [t for t, _ in tt[a1:a2]]
        dtoks = [t for t, _ in dt[b1:b2]]
        blk = (tt[a1][1] if a1 < len(tt) else tt[-1][1]) if ttoks or not dtoks else dt[b1][1]
        where = " > ".join(blk.section)
        # same characters, different splitting -> tokenizer rule; otherwise content
        nature = "split" if "".join(ttoks) == "".join(dtoks) else "content"
        ctx = " ".join(t for t, _ in tt[max(0, a1 - context):a1])
        print(f"\n  [{nature}] {len(ttoks) - len(dtoks):+d}  {where}  ({blk.kind})", file=out)
        print(f"    context: …{ctx}", file=out)
        print(f"    tex : {' '.join(ttoks) or '∅'}", file=out)
        print(f"    docx: {' '.join(dtoks) or '∅'}", file=out)
