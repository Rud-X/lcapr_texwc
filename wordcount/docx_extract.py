"""Extract the counted text of a .docx as kind-tagged blocks.

Mirrors what Word / LibreOffice count: body paragraphs, tables, field results
(TOC, SEQ captions), footnotes, headers/footers (LibreOffice counts them,
Word does not); no field instructions, no deleted or hidden text.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET

from .common import FRONT_MATTER, HEADERS, Block, Config

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def stored_word_count(path) -> int | None:
    """The <Words> statistic the saving application wrote into docProps/app.xml."""
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read("docProps/app.xml").decode("utf-8")
        except KeyError:
            return None
    m = re.search(r"<Words>(\d+)</Words>", xml)
    return int(m.group(1)) if m else None


class DocxExtractor:
    def __init__(self, cfg: Config):
        d = cfg["docx"]
        self.heading_re = re.compile(d["heading_style"])
        self.toc_re = re.compile(d["toc_style"])
        self.caption_re = re.compile(d["caption_style"])
        self.bib_re = re.compile(d["bibliography_style"])
        self.guidance_fills = {f.upper() for f in d.get("guidance_fills", [])}
        self.skip_hidden = d.get("skip_hidden", True)
        self.symbol_map = {k.upper(): v for k, v in d.get("symbol_map", {}).items()}

    def extract(self, path) -> list[Block]:
        self.blocks: list[Block] = []
        self.section: list[str] = []
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
            footnotes = z.read("word/footnotes.xml") if "word/footnotes.xml" in z.namelist() else None
            hf_parts = self._header_footer_parts(z, root)
        self._walk(root.find(W + "body"), in_table=False)
        # LibreOffice counts every header/footer variant (default, even, first) once
        for part in hf_parts:
            for p in part.iter(W + "p"):
                self.blocks.append(Block("header", self._para_text(p), (HEADERS,)))
        if footnotes is not None:
            for fn in ET.fromstring(footnotes).iter(W + "footnote"):
                if fn.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                text = " ".join(self._para_text(p) for p in fn.iter(W + "p"))
                self.blocks.append(Block("footnote", text, (FRONT_MATTER,)))
        return self.blocks

    @staticmethod
    def _header_footer_parts(z, root):
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        refs = {"header": [], "footer": []}
        for kind in refs:
            for ref in root.iter(W + kind + "Reference"):
                t = target.get(ref.get(R + "id"))
                if t and t not in refs[kind]:
                    refs[kind].append(t)
        return [ET.fromstring(z.read("word/" + t.lstrip("/").removeprefix("word/")))
                for t in refs["header"] + refs["footer"]]

    def _walk(self, el, in_table: bool):
        for child in el:
            tag = child.tag
            if tag == W + "p":
                self._paragraph(child, in_table)
            elif tag == W + "tbl":
                for tr in child.findall(W + "tr"):
                    for tc in tr.findall(W + "tc"):
                        self._walk(tc, in_table=True)
            elif tag in (W + "sdt", W + "sdtContent", W + "customXml"):
                self._walk(child, in_table)

    def _para_text(self, p) -> str:
        parts = []
        for r in p.iter(W + "r"):
            rpr = r.find(W + "rPr")
            vanish = rpr.find(W + "vanish") if rpr is not None else None
            if self.skip_hidden and vanish is not None and vanish.get(W + "val", "true") not in ("false", "0", "off"):
                continue
            for c in r:
                tag = c.tag
                if tag == W + "t":
                    parts.append(c.text or "")
                elif tag in (W + "tab", W + "br", W + "cr", W + "ptab"):
                    parts.append(" ")
                elif tag == W + "noBreakHyphen":
                    parts.append("‑")
                elif tag == W + "sym":
                    key = f"{c.get(W + 'font')}:{c.get(W + 'char')}".upper()
                    parts.append(self.symbol_map.get(key, "□"))
        return "".join(parts)

    def _paragraph(self, p, in_table: bool):
        ppr = p.find(W + "pPr")
        style = ""
        fill = ""
        if ppr is not None:
            ps = ppr.find(W + "pStyle")
            style = ps.get(W + "val") if ps is not None else ""
            shd = ppr.find(W + "shd")
            fill = (shd.get(W + "fill") or "").upper() if shd is not None else ""
        text = self._para_text(p)

        m = self.heading_re.match(style)
        if m:
            level = int(m.group(1))
            title = text.strip()
            self.section = self.section[: level - 1] + [title]
            kind = "heading"
        elif self.toc_re.match(style):
            kind = "toc"
        elif self.caption_re.match(style):
            kind = "caption"
        elif self.bib_re.match(style):
            kind = "bibliography"
        elif fill in self.guidance_fills:
            kind = "guidance"
        elif in_table:
            kind = "table"
        else:
            kind = "para"
        self.blocks.append(Block(kind, text, tuple(self.section) or (FRONT_MATTER,)))
