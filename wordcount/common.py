"""Shared data structures: config loading, text blocks, tokenizer and filtering."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FRONT_MATTER = "(front matter)"
HEADERS = "(headers/footers)"


@dataclass
class Block:
    kind: str                      # heading, para, table, caption, guidance, toc, bibliography, footnote, header
    text: str
    section: tuple[str, ...] = ()  # heading path; a heading belongs to its own section
    tokens: list[str] = field(default_factory=list)


class Config(dict):
    """The parsed TOML file, with the directory paths are resolved against."""

    def __init__(self, data: dict, base: Path):
        super().__init__(data)
        self.base = base

    def path(self, key: str) -> Path | None:
        value = self["input"].get(key)
        return (self.base / value) if value else None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path, "rb") as f:
        return Config(tomllib.load(f), path.resolve().parent)


# --------------------------------------------------------------------------- tokenizer

class Tokenizer:
    def __init__(self, cfg: Config, mode: str | None = None):
        self.mode = mode or cfg["tokenizer"]["mode"]
        rules = cfg["tokenizer"][self.mode]
        seps = rules.get("extra_separators", "")
        self.sep_re = re.compile(f"[{re.escape(seps)}]") if seps else None
        self.count_symbols = rules.get("count_symbol_only_tokens", True)
        self.ignore = [re.compile(p) for p in rules.get("ignore_tokens", [])]
        self.exclude_kinds = rules.get("exclude_kinds", [])

    def __call__(self, text: str) -> list[str]:
        if self.sep_re:
            text = self.sep_re.sub(" ", text)
        # str.split() splits on all Unicode whitespace (incl. U+00A0, U+202F), but not U+200B
        tokens = text.split()
        if not self.count_symbols:
            tokens = [t for t in tokens if re.search(r"\w", t)]
        if self.ignore:
            tokens = [t for t in tokens if not any(p.fullmatch(t) for p in self.ignore)]
        return tokens


# --------------------------------------------------------------------------- filtering

def strip_number(heading: str) -> str:
    """'8. References' -> 'References', '2.1 Goal' -> 'Goal', 'A. Extra' -> 'Extra'."""
    return re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)*\.)\s+", "", heading).strip()


def apply_filters(blocks: list[Block], cfg: Config, tokenize: Tokenizer,
                  log: list | None = None) -> list[Block]:
    """Tokenize the blocks that pass the [count] rules; return only those.
    `log` (optional) receives (block, reason) for every block, kept or dropped."""
    kinds = set(cfg["count"]["kinds"]) - set(tokenize.exclude_kinds)
    excl = [re.compile(p) for p in cfg["count"].get("exclude_sections", [])]
    stop = cfg["count"].get("stop_at_heading", "")
    stop_re = re.compile(stop) if stop else None
    stopped = False
    kept = []

    def note(b: Block, reason: str):
        if log is not None:
            log.append((b, reason))

    for b in blocks:
        # everything from the first matching heading on is dropped; page headers/footers
        # are not part of the text flow and stay governed by `kinds`
        if stop_re and b.kind == "heading" and stop_re.fullmatch(strip_number(b.text)):
            stopped = True
        if stopped and b.kind != "header":
            note(b, f"after stop heading {stop!r}")
            continue
        if b.kind not in kinds:
            note(b, f"kind {b.kind!r} not counted")
            continue
        if (p := next((p for p in excl for title in b.section if p.search(title)), None)):
            note(b, f"excluded section {p.pattern!r}")
            continue
        b.tokens = tokenize(b.text)
        if b.tokens:
            kept.append(b)
            note(b, "kept")
        else:
            note(b, "no tokens")
    return kept
