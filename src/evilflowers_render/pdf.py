"""The two PDFs of a book: the distribution copy (``pdf``) and the PDF/A-2b master (``pdfa``).

Ported from evilflowers-books-digitalizer (98bf9b0), behaviour unchanged:

* ``pipeline/profiles.py``: :class:`PdfProfile`, :data:`DISTRIBUTION`, :data:`ARCHIVAL`;
* ``pipeline/steps/render.py``: :func:`recode` (``RenderPdf``), one ``recode_pdf`` per profile;
* ``pipeline/steps/finalize.py``: :class:`Finalizer` (``FinalizePdf``): outline, page labels, linearization;
* ``pipeline/pdfa.py`` + ``pipeline/steps/pdfa.py``: :func:`to_pdfa2b`, :func:`validate_pdfa`.

The dual-output fix stays: the distribution PDF carries **JPEG** MRC layers, the archival master
**JPEG 2000**. JPEG 2000 inside the distribution PDF rendered white and slowly in viewers
(the digitalizer's ``jpeg2000-output-bug`` note).

What changed for the service: every subprocess has a timeout (the time the book has left), and a
timeout raises :class:`~evilflowers_render.deadline.DeadlineExceeded`; veraPDF has its own
``required`` switch instead of the CLI's silent skip.
"""

from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pikepdf

from evilflowers_render.deadline import Deadline, DeadlineExceeded
from evilflowers_render.hocr import HocrLine, parse_hocr

logger = logging.getLogger(__name__)

ImageFormat = Literal["jpeg", "jpeg2000"]
MaskCompression = Literal["jbig2", "ccitt"]
DenoiseMask = Literal["none", "fast", "bregman"]

#: ``recode_pdf`` requires this binary on ``PATH`` for ``--mrc-image-format jpeg``.
JPEG_REQUIRES = "jpegoptim"
#: The digitalizer's ``RenderPdf`` default; it lands in the XMP CreatorTool, so it must not drift.
CREATOR = "EvilFlowers Books Digitalizer"


class RenderError(RuntimeError):
    """A tool failed or is missing; the message goes into the ``failed`` outcome."""


# -- profiles -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PdfProfile:
    """One ``recode_pdf`` rendering recipe (digitalizer ``pipeline/profiles.py``)."""

    name: str
    image_format: ImageFormat = "jpeg"
    bg_downsample: int | None = None
    fg_downsample: int | None = None
    downsample: int | None = None
    mask_compression: MaskCompression = "jbig2"
    denoise_mask: DenoiseMask = "fast"
    jpeg2000_encoder: str = "pillow"  # -J; only meaningful for image_format="jpeg2000"
    hq_pages: str | None = "1,2,3,4,-4,-3,-2,-1"
    bg_compression_flags: str | None = None
    fg_compression_flags: str | None = None
    linearize: bool = True
    pdfa: str | None = None

    def recode_args(self) -> list[str]:
        """Profile-specific ``recode_pdf`` flags (the renderer adds I/O + dpi)."""
        args: list[str] = ["--mrc-image-format", self.image_format]
        if self.image_format == "jpeg2000":
            args += ["-J", self.jpeg2000_encoder]
        args += ["--mask-compression", self.mask_compression]
        args += ["--denoise-mask", self.denoise_mask]
        if self.downsample is not None:
            args += ["--downsample", str(self.downsample)]
        if self.bg_downsample is not None:
            args += ["--bg-downsample", str(self.bg_downsample)]
        if self.fg_downsample is not None:
            args += ["--fg-downsample", str(self.fg_downsample)]
        if self.hq_pages:
            args += ["--hq-pages", self.hq_pages]
        if self.bg_compression_flags:
            args += [f"--bg-compression-flags={self.bg_compression_flags}"]
        if self.fg_compression_flags:
            args += [f"--fg-compression-flags={self.fg_compression_flags}"]
        return args


#: Fast-everywhere access copy. fg_downsample stays unset: recode_pdf 1.5.7 corrupts the
#: foreground/text layer when --fg-downsample is passed.
DISTRIBUTION = PdfProfile(name="distribution", image_format="jpeg", bg_downsample=3, denoise_mask="fast",
                          linearize=True, pdfa=None)  # fmt: skip
#: Preservation master: JPEG 2000 MRC -> PDF/A-2b.
ARCHIVAL = PdfProfile(name="archival", image_format="jpeg2000", bg_downsample=2, jpeg2000_encoder="pillow",
                      linearize=False, pdfa="2b")  # fmt: skip

#: output format -> profile (the digitalizer's ``StudioRenderer.PDF_FORMATS``)
PDF_FORMATS: dict[str, PdfProfile] = {"pdf": DISTRIBUTION, "pdfa": ARCHIVAL}


# -- recode_pdf -----------------------------------------------------------------------------------------------------


def metadata_args(meta: dict[str, Any], creator: str = CREATOR) -> list[str]:
    """Document metadata baked into recode_pdf's XMP (rewriting it with pikepdf breaks PDF/A)."""
    args = ["--metadata-creatortool", creator]
    title = meta.get("title")
    if title:
        args += ["--metadata-title", str(title)]
    authors = meta.get("authors") or []
    if authors:
        args += ["--metadata-author", "; ".join(authors)]
    language = meta.get("language")
    if language:
        args += ["--metadata-language", str(language)]
    return args


def recode_bin(override: str | None = None) -> str:
    if override:
        return override
    candidate = Path(sys.executable).parent / "recode_pdf"
    if candidate.exists():
        return str(candidate)
    path = shutil.which("recode_pdf")
    if path is None:
        raise RenderError("recode_pdf not found: install the archive-pdf-tools dependency")
    return path


def page_glob(pages: list[Path]) -> str:
    """The pages share one directory and extension, and nothing else is in it; the glob recode_pdf reads."""
    # resolve(): Leptonica chokes on macOS /tmp symlink paths
    resolved = sorted(p.resolve() for p in pages)
    if not resolved:
        raise RenderError("nothing to render: the book has no access leaves")
    pages_dir, suffix = resolved[0].parent, resolved[0].suffix
    if any(p.parent != pages_dir or p.suffix != suffix for p in resolved):
        raise RenderError("pages must share one directory and extension")
    stray = set(pages_dir.glob(f"*{suffix}")) - set(resolved)
    if stray:
        raise RenderError(f"stray images next to the pages: {sorted(stray)[:3]}")
    return str(pages_dir / f"*{suffix}")


def recode(
    profile: PdfProfile,
    pages: list[Path],
    hocr: Path,
    out: Path,
    *,
    dpi: int,
    meta: dict[str, Any],
    deadline: Deadline,
    recode: str | None = None,
) -> Path:
    """One profile's MRC PDF at ``out`` (``RenderPdf.run`` for one profile)."""
    if profile.image_format == "jpeg" and not shutil.which(JPEG_REQUIRES):
        raise RenderError(f"{JPEG_REQUIRES!r} not found: required by recode_pdf for JPEG MRC output")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [recode_bin(recode), "-I", page_glob(pages), "-T", str(hocr), "-o", str(out), "-D", str(dpi)]
    cmd += profile.recode_args() + metadata_args(meta)
    deadline.check()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline.remaining())
    except subprocess.TimeoutExpired as error:
        raise DeadlineExceeded(f"recode_pdf ({profile.name}) passed the book's deadline") from error
    if result.returncode != 0 or not out.exists():
        raise RenderError(f"recode_pdf ({profile.name}) failed (exit {result.returncode}): {result.stderr[-2000:]}")
    logger.info("%s PDF %.2f MB (%s)", profile.name, out.stat().st_size / 1e6, profile.image_format)
    return out


# -- finalize: outline, page labels, linearization ------------------------------------------------------------------

_ARABIC = re.compile(r"^\d{1,4}$")
#: characters that betray a table ruling, caption, author line or OCR misread, not a chapter heading
_NOISE_CHARS = set("|[]{}<>=_~\\:;,")


@dataclass(frozen=True, slots=True)
class Finalizer:
    """Bookmarks + page labels and (optionally) linearization for each PDF (``FinalizePdf``)."""

    bookmarks: bool = True
    page_labels: bool = True
    linearize: bool = True
    # heading heuristics, calibrated on the lab06 fad sample:
    size_ratio: float = 1.18  # heading x_size >= body median * ratio
    max_words: int = 6  # per merged title
    alpha_ratio: float = 0.78  # letters / non-space chars
    max_per_page: int = 2
    # printed page numbers live within this margin band (fraction of height)
    edge_band: float = 0.08
    min_number_hits: int = 3  # pages agreeing on one offset before we trust it

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"[^\w)]+$", "", text.strip())

    def _is_heading_line(self, line: HocrLine, body_size: float) -> bool:
        if line.size < body_size * self.size_ratio:
            return False
        text = self._clean(line.text)
        words = text.split()
        if not words or len(words) > self.max_words:
            return False
        if _NOISE_CHARS & set(text):
            return False
        if not (text[0].isupper() or text[0].isdigit()):
            return False
        if text.endswith("-"):  # hyphenated continuation, not a heading start
            return False
        if words[-1].isdigit():  # table scores / page refs ("... — 1")
            return False
        # a lone capital inside a mixed-case line is OCR debris, not an initial
        if not text.isupper() and any(len(w) == 1 and w.isupper() for w in words):
            return False
        letters = sum(ch.isalpha() for ch in text)
        non_space = sum(not ch.isspace() for ch in text)
        return letters >= 4 and non_space > 0 and letters / non_space >= self.alpha_ratio

    def detect_headings(self, lines: list[HocrLine]) -> list[tuple[int, str]]:
        """One bookmark per heading, merging wrapped lines on the same page."""
        body_sizes = [ln.size for ln in lines if len(ln.text.split()) >= 3]
        if len(body_sizes) < 10:
            return []
        body = statistics.median(body_sizes)
        found: list[tuple[int, str]] = []
        per_page: dict[int, int] = {}
        i = 0
        while i < len(lines):
            if not self._is_heading_line(lines[i], body):
                i += 1
                continue
            page = lines[i].page
            parts = [self._clean(lines[i].text)]
            ref_size = lines[i].size
            j = i + 1
            # absorb following lines on the same page of similar size (wrapped title)
            while (
                j < len(lines)
                and lines[j].page == page
                and abs(lines[j].size - ref_size) <= ref_size * 0.15
                and self._is_heading_line(lines[j], body)
            ):
                parts.append(self._clean(lines[j].text))
                j += 1
            title = " ".join(parts)
            words = title.split()
            if (
                1 <= len(words) <= self.max_words
                and per_page.get(page, 0) < self.max_per_page
                and (not found or found[-1] != (page, title))  # dedup
            ):
                found.append((page, title))
                per_page[page] = per_page.get(page, 0) + 1
            i = j
        return found

    def detect_page_offset(self, lines: list[HocrLine], page_dims: list[tuple[int, int]]) -> int | None:
        votes: dict[int, int] = {}
        for line in lines:
            if line.page >= len(page_dims):
                continue
            _w, h = page_dims[line.page]
            if not h:
                continue
            top, bottom = line.bbox[1], line.bbox[3]
            if top > h * self.edge_band and bottom < h * (1 - self.edge_band):
                continue
            text = line.text.strip()
            if not _ARABIC.fullmatch(text):
                continue  # roman front matter handled via the arabic offset
            printed = int(text)
            votes[printed - line.page] = votes.get(printed - line.page, 0) + 1
        if not votes:
            return None
        offset, hits = max(votes.items(), key=lambda kv: kv[1])
        return offset if hits >= self.min_number_hits else None

    @staticmethod
    def apply_page_labels(pdf: pikepdf.Pdf, offset: int) -> None:
        nums: list = []
        start = max(0, 1 - offset)  # first physical page with printed number >= 1
        if start > 0:
            nums += [0, pikepdf.Dictionary(S=pikepdf.Name.r)]
        nums += [start, pikepdf.Dictionary(S=pikepdf.Name.D, St=start + offset)]
        pdf.Root.PageLabels = pikepdf.Dictionary(Nums=pikepdf.Array(nums))

    def run(self, outputs: list[tuple[PdfProfile, Path]], hocr: Path | None) -> None:
        """Polish every produced PDF in place; never fails over a heuristic."""
        lines: list[HocrLine] = []
        page_dims: list[tuple[int, int]] = []
        if hocr is not None and hocr.exists():
            doc = parse_hocr(hocr.read_text(errors="ignore"))
            lines, page_dims = doc.lines, doc.page_dims
        elif self.bookmarks or self.page_labels:
            logger.warning("no hOCR: skipping bookmarks and page labels")
        headings = self.detect_headings(lines) if (self.bookmarks and lines) else []
        offset = self.detect_page_offset(lines, page_dims) if (self.page_labels and lines) else None
        for profile, pdf_path in outputs:
            with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                n_pages = len(pdf.pages)
                page_headings = [(p, t) for p, t in headings if p < n_pages]
                if page_headings:
                    with pdf.open_outline() as outline:
                        outline.root.extend(pikepdf.OutlineItem(title, page) for page, title in page_headings)
                if offset is not None:
                    self.apply_page_labels(pdf, offset)
                pdf.save(pdf_path, linearize=profile.linearize)
        if headings:
            logger.info("%d outline bookmarks", len(headings))


# -- PDF/A ----------------------------------------------------------------------------------------------------------


def _has_embedded_files(pdf: pikepdf.Pdf) -> bool:
    names = pdf.Root.get("/Names")
    return names is not None and pikepdf.Name.EmbeddedFiles in names


def to_pdfa2b(pdf_path: Path) -> bool:
    """Re-declare recode_pdf's PDF/A-3b file as PDF/A-2b in place (lossless: only the XMP part id).

    ``False`` when the file embeds attachments (a genuine PDF/A-3 feature that must stay part 3).
    """
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        if _has_embedded_files(pdf):
            logger.warning("%s embeds files: keeping PDF/A-3b", pdf_path.name)
            return False
        with pdf.open_metadata() as meta:
            meta["pdfaid:part"] = "2"
            meta["pdfaid:conformance"] = "B"
        pdf.save(pdf_path)
    return True


def ensure_pdfa(outputs: list[tuple[PdfProfile, Path]]) -> None:
    """``EnsurePdfA``: the PDF/A part identifier is the last touch on every ``pdfa``-flagged output."""
    for profile, pdf_path in outputs:
        if not profile.pdfa:
            continue
        if profile.pdfa != "2b":
            logger.warning("%s: only PDF/A-2b is supported, got %r; skipping", profile.name, profile.pdfa)
            continue
        to_pdfa2b(pdf_path)


def validate_pdfa(
    pdf_path: Path, *, deadline: Deadline, flavour: str = "2b", verapdf: str = "verapdf", required: bool = True
) -> bool | None:
    """veraPDF's verdict; ``None`` only when veraPDF is missing and not ``required``."""
    try:
        command = [verapdf, "--flavour", flavour, str(pdf_path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=deadline.remaining())
    except FileNotFoundError as error:
        if required:
            raise RenderError(f"veraPDF ({verapdf}) not found: the PDF/A check is required") from error
        logger.warning("veraPDF not installed: skipping PDF/A validation of %s", pdf_path.name)
        return None
    except subprocess.TimeoutExpired as error:
        raise DeadlineExceeded("veraPDF passed the book's deadline") from error
    compliant = 'isCompliant="true"' in result.stdout
    if not compliant:
        logger.warning("%s is NOT PDF/A-%s compliant", pdf_path.name, flavour)
    return compliant
