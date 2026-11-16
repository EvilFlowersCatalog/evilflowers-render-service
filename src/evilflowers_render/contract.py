"""The contract of ``evilflowers_render.book`` (evilflowers-render-service#1, the contract comment of 2026-11-16).

Message: the kit's ``StandaloneJob`` (``run_id``, ``job``, ``token``, ``inputs``, ``output_dir``, ``outcome``)
plus ``request``, a JSON file the orchestrator writes next to the item. Redis carries only paths;
every path, in the message and in the request, is relative to ``STORAGE_PATH``.

Outcome: ``run_id``, ``token``, ``status``, ``error``, ``pdfa_valid`` and ``files`` (kind -> path,
size, md5), written last and atomically. The orchestrator applies it only when every requested
format is present, no other kind appears (``images`` comes with a non-empty ``request.jp2``), each
file is inside this attempt's ``output_dir`` with the stated size, and ``pdfa_valid`` is not false.
"""

from __future__ import annotations

from typing import Any, Literal

from evilflowers_worker.contract import StandaloneJob
from evilflowers_worker.outcome import OutcomeFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_NAME = "evilflowers_render.book"


class BookJob(StandaloneJob):
    """The task's kwargs. ``inputs`` holds the request (``contract.input_paths`` in the digitalizer)."""

    #: The request file, relative to ``STORAGE_PATH``.
    request: str = Field(min_length=1)


class RenderRequest(BaseModel):
    """``<item>/<item>_studio/render/request-<run>.json``, written by the orchestrator's ``prepare`` step.

    ``pages``, ``hocr`` and ``regions`` cover **access leaves only**, in order: ``pages[i]`` <->
    ``regions[i]`` <-> the i-th ``ocr_page``. ``jp2`` lists **all** leaves; empty means: no IA page package.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    item_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    formats: list[Literal["pdf", "pdfa", "epub"]] = Field(min_length=1)
    pages: list[str] = Field(min_length=1)
    hocr: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    dpi: int | None = Field(None, gt=0)
    #: one list of region rows per access page (page pixels); ``None``: heuristics only
    regions: list[list[dict[str, Any]] | None] | None = None
    jp2: list[str] = Field(default_factory=list)
    leaves: list[int] = Field(default_factory=list)
    n_leaves: int | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RenderRequest:
        if len(set(self.formats)) != len(self.formats):
            raise ValueError(f"formats repeat a kind: {self.formats}")
        if self.regions is not None and len(self.regions) != len(self.pages):
            raise ValueError(f"{len(self.regions)} region lists for {len(self.pages)} access pages")
        return self

    def paths(self) -> list[str]:
        """Every file the job reads, for the storage check."""
        return [*self.pages, self.hocr, *self.jp2]


class RenderedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str  # in output_dir, relative to STORAGE_PATH
    size: int
    md5: str


class BookOutcome(OutcomeFile):
    status: Literal["done", "failed"]
    error: str | None = None
    #: veraPDF's verdict on ``<id>_archival.pdf``; null when ``pdfa`` was not requested
    pdfa_valid: bool | None = None
    #: kind (pdf | pdfa | epub | images) -> file
    files: dict[str, RenderedFile] = Field(default_factory=dict)


class OutcomeAddress(BaseModel):
    """Enough of a message to write a ``failed`` outcome when the rest of it is invalid."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    run_id: str = Field(min_length=1)
    token: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
