"""``evilflowers_render.book``: one book's request -> media in ``output_dir`` -> the outcome file, last.

The behaviour of the digitalizer's ``StudioRenderer`` and of its executable spec
``studio/testing/standalone.py::render_book`` (98bf9b0), as a kit standalone job:

1. the message and every path of the request are checked to stay under ``STORAGE_PATH``;
2. the access pages are hard-linked into a private ``output_dir/.render-<id>/pages`` (recode_pdf
   reads a glob and refuses stray files);
3. ``pdf`` and ``pdfa``: one recode_pdf per profile, then outline + page labels on both, then the
   PDF/A-2b declaration on the master; veraPDF checks it;
4. ``epub``: the reflow edition steered by the request's region rows;
5. ``images``: ``<id>_jp2.zip`` from ``request.jp2`` when it is not empty;
6. the outcome (size + md5 per file, the PDF/A verdict), last and atomic.

Only media: ``_hocr.html``, ``_djvu.txt`` and the IA XML documents are the orchestrator's.

Failures write ``status: failed`` with ``error``, then raise (Celery fires ``link_error``). A
deliberate stop (SIGTERM, cold cancel: ``SystemExit``) writes nothing, so the redelivery can.
Unlike the digitalizer's CLI, a failed EPUB fails the job: the orchestrator refuses an outcome
without every requested format, so a ``done`` without the EPUB would only fail later and vaguer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from evilflowers_worker import observability
from evilflowers_worker.observability.metrics import record_job
from evilflowers_worker.outcome import write_outcome
from pydantic import ValidationError

from evilflowers_render import pdf as pdfs
from evilflowers_render.contract import BookJob, BookOutcome, OutcomeAddress, RenderedFile, RenderRequest
from evilflowers_render.deadline import Deadline, DeadlineExceeded
from evilflowers_render.jp2 import build_jp2_zip
from evilflowers_render.metadata import classic_metadata
from evilflowers_render.naming import ia_name
from evilflowers_render.regions import region_decisions
from evilflowers_render.settings import Settings, get_settings

logger = logging.getLogger(__name__)

METRIC_JOB = "book"


class InvalidRequest(ValueError):
    """The message or its request file breaks the contract."""


class RenderFailed(RuntimeError):
    """The outcome is ``failed``; raised after the outcome file is written, so the task fails (``link_error``)."""


class GaveUp(RuntimeError):
    """Past MAX_DELIVERIES: the book killed its worker on every delivery; raised after the ``failed`` outcome."""


# -- entry points ---------------------------------------------------------------------------------------------------


def run(job: BookJob, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    check_outcome_path(job, settings)
    started = time.perf_counter()
    deadline = Deadline(settings.task_soft_time_limit)
    with observability.span("render.book") as span, observability.bind(run_id=job.run_id, job=job.job):
        logger.info("render started -> %s", job.output_dir)
        try:
            outcome = render(job, settings, deadline)
        # Exception only: SystemExit (a deliberate stop) must propagate and never become a `failed` outcome.
        except Exception as error:
            logger.exception("render failed")
            message = describe(error, settings)
            write_failed(job, settings, message)
            if span is not None:
                observability.fail(error)
            raise RenderFailed(message) from error
        write_outcome(settings, job.outcome, outcome)
        record_job(METRIC_JOB, "done")
        logger.info("render done in %.0f s: %s", time.perf_counter() - started, ", ".join(sorted(outcome.files)))
        return {"job": job.job, "status": "done", "files": sorted(outcome.files), "pdfa_valid": outcome.pdfa_valid}


def give_up(job: BookJob, delivery: int, settings: Settings | None = None) -> None:
    """Past the delivery cap: a ``failed`` outcome (the orchestrator ends the job, it does not re-send it)."""
    settings = settings or get_settings()
    check_outcome_path(job, settings)
    error = f"gave up after {delivery - 1} deliveries: the worker died while rendering this book every time"
    with observability.bind(run_id=job.run_id, job=job.job):
        write_failed(job, settings, error)
        logger.error("render %s (outcome: %s)", error, job.outcome)
        raise GaveUp(error)


def write_failed(job: BookJob | OutcomeAddress, settings: Settings, error: str) -> None:
    outcome = BookOutcome(run_id=job.run_id, token=job.token, status="failed", error=error)
    write_outcome(settings, job.outcome, outcome)
    record_job(METRIC_JOB, "failed")


def check_outcome_path(job: BookJob | OutcomeAddress, settings: Settings) -> Path:
    try:
        return settings.storage_file(job.outcome)
    except ValueError as error:
        raise InvalidRequest(f"outcome: {error}") from error


def describe(error: Exception, settings: Settings) -> str:
    if isinstance(error, DeadlineExceeded):
        return f"time limit exceeded ({settings.task_soft_time_limit} s): {error}"
    if isinstance(error, InvalidRequest | pdfs.RenderError | FileNotFoundError):
        return str(error)
    return f"{type(error).__name__}: {error}"


# -- one book -------------------------------------------------------------------------------------------------------


def load_request(job: BookJob, settings: Settings) -> tuple[RenderRequest, dict[str, Path]]:
    """The request and every file it names, as absolute paths checked to stay under ``STORAGE_PATH``."""
    try:
        request_path = settings.storage_file(job.request)
    except ValueError as error:
        raise InvalidRequest(f"request: {error}") from error
    if job.request not in job.inputs:
        raise InvalidRequest(f"request {job.request!r} is not among the job's inputs")
    try:
        request = RenderRequest.model_validate(json.loads(request_path.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise InvalidRequest(f"request {job.request!r} does not exist") from error
    except (ValueError, ValidationError) as error:
        raise InvalidRequest(f"request {job.request!r} breaks the contract: {error}") from error
    resolved: dict[str, Path] = {}
    for relative in request.paths():
        try:
            path = settings.storage_file(relative)
        except ValueError as error:
            raise InvalidRequest(f"request path {relative!r}: {error}") from error
        if not path.is_file():
            raise InvalidRequest(f"request path {relative!r} does not exist")
        resolved[relative] = path
    return request, resolved


def render(job: BookJob, settings: Settings, deadline: Deadline) -> BookOutcome:
    request, paths = load_request(job, settings)
    out = job.prepare_output(settings)
    item = request.item_id
    targets = {fmt: job.output_file(settings, ia_name(item, fmt)) for fmt in request.formats}
    work = out / f".render-{item}"
    if work.exists():
        shutil.rmtree(work)
    files: dict[str, Path] = {}
    pdfa_valid: bool | None = None
    with observability.bind(item_id=item):
        try:
            pages = stage_pages([paths[p] for p in request.pages], work / "pages", check=deadline.check)
            hocr = paths[request.hocr].resolve()
            meta = classic_metadata(request.metadata)
            files |= render_pdfs(request, pages, hocr, work, targets, meta, settings, deadline)
            if "pdfa" in files:
                pdfa_valid = pdfs.validate_pdfa(
                    files["pdfa"], deadline=deadline, verapdf=settings.verapdf, required=settings.verapdf_required
                )
            if "epub" in request.formats:
                files["epub"] = render_epub(request, pages, hocr, work, targets["epub"], meta, deadline)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if request.jp2:
            images = job.output_file(settings, ia_name(item, "images"))
            files["images"] = build_jp2_zip(item, [paths[p] for p in request.jp2], images, check=deadline.check)
    missing = set(request.formats) - set(files)
    if missing:
        raise pdfs.RenderError(f"no output for {sorted(missing)}")
    root = settings.storage_root
    rendered = {kind: describe_file(path, root) for kind, path in files.items()}
    return BookOutcome(
        run_id=job.run_id, token=job.token, status="done", error=None, pdfa_valid=pdfa_valid, files=rendered
    )


def stage_pages(pages: list[Path], dest: Path, *, check=None) -> list[Path]:
    """Hard-link (copy across devices) pages into ``dest`` as ``0000<ext>``, ``0001<ext>`` …"""
    if not pages:
        raise InvalidRequest("nothing to render: the book has no access leaves")
    suffix = pages[0].suffix
    if any(p.suffix != suffix for p in pages):
        raise InvalidRequest("access leaves must share one image format")
    dest.mkdir(parents=True)
    staged = []
    for i, src in enumerate(pages):
        if check is not None:
            check()
        dst = dest / f"{i:04d}{suffix}"
        try:
            os.link(src.resolve(), dst)
        except OSError:
            shutil.copy2(src, dst)
        staged.append(dst)
    return staged


def render_pdfs(
    request: RenderRequest,
    pages: list[Path],
    hocr: Path,
    work: Path,
    targets: dict[str, Path],
    meta: dict[str, Any],
    settings: Settings,
    deadline: Deadline,
) -> dict[str, Path]:
    """``pdf`` and ``pdfa``: recode per profile, finalize both, then the PDF/A declaration (the last touch)."""
    profiles = [(fmt, pdfs.PDF_FORMATS[fmt]) for fmt in request.formats if fmt in pdfs.PDF_FORMATS]
    if not profiles:
        return {}
    dpi = int(request.dpi or settings.default_dpi)
    built: list[tuple[pdfs.PdfProfile, Path]] = []
    for _fmt, profile in profiles:
        # The digitalizer's flat layout: <work>/out/studio_<id>.<profile>.pdf
        out = work / "out" / f"studio_{request.item_id}.{profile.name}.pdf"
        pdfs.recode(profile, pages, hocr, out, dpi=dpi, meta=meta, deadline=deadline, recode=settings.recode_pdf)
        built.append((profile, out))
    deadline.check()
    if settings.finalize:
        pdfs.Finalizer().run(built, hocr)
    pdfs.ensure_pdfa(built)
    produced: dict[str, Path] = {}
    for (fmt, _profile), (_p, path) in zip(profiles, built, strict=True):
        shutil.move(path, targets[fmt])
        produced[fmt] = targets[fmt]
    return produced


def render_epub(
    request: RenderRequest,
    pages: list[Path],
    hocr: Path,
    work: Path,
    target: Path,
    meta: dict[str, Any],
    deadline: Deadline,
) -> Path:
    """The reflow edition, steered by the operator's region decisions (no VLM: formulas use the stored LaTeX)."""
    from evilflowers_render.reflow import ReflowSettings, build_edition

    deadline.check()
    build_edition(
        pages,
        hocr,
        target,
        work / "reflow",
        meta,
        ReflowSettings(),
        decisions=region_decisions(request.regions, len(pages)),
        check=deadline.check,
    )
    if not target.is_file():
        raise pdfs.RenderError("the EPUB was not written")
    return target


def describe_file(path: Path, root: Path) -> RenderedFile:
    """Path relative to the storage root, size and md5 (streamed: a book's PDF can be gigabytes)."""
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return RenderedFile(path=path.resolve().relative_to(root).as_posix(), size=path.stat().st_size,
                        md5=digest.hexdigest())  # fmt: skip
