"""LaTeX hygiene for the reflow edition: the non-VLM half of the digitalizer's ``reflow/vlm.py``.

The render service never runs the vision model (formulas use the LaTeX the
formulas stage stored), so only the answer-keying and the output guards are
ported here, unchanged:

* :class:`Job` — its ``key`` (``page + task + bbox``) keys formula answers;
* :func:`looks_hallucinated` — a drawing read as "math" (stacked arrays of
  lone labels, caption text inside math);
* :func:`fix_latex` — normalizes environments texmath/LaTeX accept;
* :func:`validate_math` — one batched pandoc run proves which snippets become
  MathML; the rest fall back (image crop or Tesseract text).

:class:`VlmSettings` is kept only so :class:`~evilflowers_render.reflow.ReflowSettings`
stays field-compatible; enabling it is an error (see ``edition.build_edition``).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evilflowers_render.reflow.model import Box

logger = logging.getLogger(__name__)

type Task = Literal["ocr", "formula"]


@dataclass(frozen=True, slots=True)
class Job:
    page: int
    image: Path
    bbox: Box
    task: Task

    @property
    def key(self) -> str:
        return f"p{self.page:04d}_{self.task}_" + "_".join(str(v) for v in self.bbox)


@dataclass(slots=True)
class VlmSettings:
    """Field-compatible with the digitalizer's ``VlmSettings``; not supported here."""

    python: Path
    model: Path
    device: str = "auto"
    timeout_s: int = 6 * 3600


# ---------------------------------------------------------------- LaTeX hygiene
def strip_delims(tex: str) -> str:
    tex = tex.strip()
    for open_, close in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)"), ("$", "$")):
        if tex.startswith(open_) and tex.endswith(close) and len(tex) > len(open_) + len(close):
            return tex[len(open_):-len(close)].strip()
    return tex


def fix_latex(tex: str) -> str:
    tex = strip_delims(tex)
    tex = re.sub(r"\\eqno\s*\{?\(?([\w.]+)\)?\}?", r"\\tag{\1}", tex)   # texmath knows \tag
    for env in ("align*", "align"):
        tex = tex.replace(f"\\begin{{{env}}}", "\\begin{aligned}").replace(f"\\end{{{env}}}", "\\end{aligned}")
    return tex


def split_tag(tex: str) -> tuple[str, str]:
    """``\\tag{X}`` is display-only: lift it out (returns tex-without-tag, X)."""
    m = re.search(r"\\tag\{([^}]*)\}", tex)
    return re.sub(r"\s*\\tag\{[^}]*\}", "", tex).strip(), (m[1] if m else "")


def looks_hallucinated(tex: str) -> bool:
    """Formula-model output that describes a drawing rather than math."""
    if re.search(r"Obr\.|Obrázok|Tab\.", tex):
        return True
    arrays = re.findall(r"\\begin\{array\}\{[lcr]+\}(.*?)\\end\{array\}", tex, re.S)
    if len(arrays) >= 2:
        cells = [c.strip() for a in arrays for c in re.split(r"\\\\|&", a) if c.strip()]
        # a LaTeX command (\beta) is one symbol; lone labels are 1-2 symbols
        size = [len(re.sub(r"[\\{}_^ ]", "", re.sub(r"\\[a-zA-Z]+", "x", c))) for c in cells]
        if cells and sum(n <= 2 for n in size) / len(cells) > 0.7:
            return True
    return bool(re.search(r"\\xrightarrow|\\longrightarrow", tex)) and len(tex) < 40


def validate_math(snippets: set[str], pandoc: str = "pandoc") -> set[str]:
    """Return the subset of display-math snippets pandoc/texmath converts to MathML.

    One pandoc call for the whole book: each snippet is a numbered paragraph; a
    snippet whose paragraph holds ``<math`` converted, otherwise texmath gave up.
    Inline snippets are prefixed with ``"$"`` by the caller and checked inline.
    """
    items = sorted(snippets)
    if not items:
        return set()
    md = "\n\n".join(
        (f"S{i}: ${s[1:]}$" if s.startswith("$") else f"S{i}:\n\n$${s}$$")
        for i, s in enumerate(items)
    )
    proc = subprocess.run([pandoc, "-f", "markdown", "-t", "html", "--mathml"],
                          input=md, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning("pandoc math validation failed: %s", proc.stderr[-500:])
        return set()
    html = proc.stdout
    ok = set()
    marks = [m.start() for m in re.finditer(r"<p>S\d+:", html)] + [len(html)]
    for start, end in zip(marks, marks[1:], strict=False):
        idx = int(re.match(r"<p>S(\d+):", html[start:])[1])  # type: ignore[index]
        if "<math" in html[start:end]:
            ok.add(items[idx])
    return ok
