"""Get a live LibreOffice Writer word count for documents or text snippets.

Run it with a Python that has LibreOffice's UNO bindings: /usr/bin/python3 with
python3-uno / libreoffice-pyuno on Linux, LibreOffice's own program\python.exe on Windows.

    python3 wordcount/lo_count.py file.docx [file2.odt ...]
    python3 wordcount/lo_count.py --text "2026–2027" --text "a / b"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uno  # type: ignore
from com.sun.star.beans import PropertyValue  # type: ignore


def _prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


class LibreOffice:
    """Context manager running a private headless soffice instance."""

    def __init__(self, soffice: str = "soffice", port: int = 2083):
        self.soffice = soffice
        self.port = port
        self.profile = tempfile.mkdtemp(prefix="lo_profile_")
        self.proc = None
        self.desktop = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [self.soffice, "--headless", "--invisible", "--norestore", "--nologo",
             f"-env:UserInstallation={Path(self.profile).as_uri()}",
             f"--accept=socket,host=localhost,port={self.port};urp;"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local)
        url = f"uno:socket,host=localhost,port={self.port};urp;StarOffice.ComponentContext"
        for _ in range(120):
            try:
                ctx = resolver.resolve(url)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("could not connect to soffice")
        self.desktop = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx)
        return self

    def __exit__(self, *exc):
        try:
            self.desktop.terminate()
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _stats(self, doc) -> dict:
        # Force a layout so statistics are up to date
        doc.getCurrentController() if hasattr(doc, "getCurrentController") else None
        doc.refresh() if hasattr(doc, "refresh") else None
        stats = {s.Name: s.Value for s in doc.getDocumentProperties().DocumentStatistics}
        try:
            stats["WordCountLive"] = doc.getPropertyValue("WordCount")
        except Exception:
            pass
        return stats

    def count_file(self, path: str) -> dict:
        url = uno.systemPathToFileUrl(os.path.abspath(path))
        doc = self.desktop.loadComponentFromURL(url, "_blank", 0, (_prop("Hidden", True),))
        try:
            return self._stats(doc)
        finally:
            doc.close(True)

    def count_text(self, text: str) -> dict:
        doc = self.desktop.loadComponentFromURL(
            "private:factory/swriter", "_blank", 0, (_prop("Hidden", True),))
        try:
            body = doc.getText()
            for i, para in enumerate(text.split("\n")):
                if i:
                    body.insertControlCharacter(body.getEnd(), 0, False)  # PARAGRAPH_BREAK
                body.insertString(body.getEnd(), para, False)
            return self._stats(doc)
        finally:
            doc.close(True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--text", action="append", default=[], help="count a text snippet")
    ap.add_argument("--soffice", default="soffice", help="path of the soffice executable")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with LibreOffice(args.soffice) as lo:
        for f in args.files:
            s = lo.count_file(f)
            print(f"{s.get('WordCountLive', s.get('WordCount'))}\t{f}")
        for t in args.text:
            s = lo.count_text(t)
            print(f"{s.get('WordCountLive', s.get('WordCount'))}\t{t!r}")


if __name__ == "__main__":
    sys.exit(main())
