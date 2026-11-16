"""The request's region rows -> the decisions the EPUB honours.

Ported from evilflowers-books-digitalizer ``studio/adapters/render/__init__.py`` (98bf9b0)
``_decision`` / ``region_decisions``, with the studio's enum values as plain strings.
"""

from __future__ import annotations

from typing import Any

from evilflowers_render.reflow.edition import RegionDecision

# studio/domain.py values
PROPOSED, CONFIRMED = "proposed", "confirmed"
KEEP, REMOVE, AS_IMAGE = "keep", "remove", "as_image"
FORMULA, PICTURE = "formula", "picture"


def decision(region: Any) -> RegionDecision | None:
    """One region row -> what the EPUB honours, or None.

    Confirmed regions always count. A detector's *proposed* region counts only when it changes the
    edition by itself (a removal, an image crop or a formula, whose LaTeX the formulas stage stored);
    disputed and rejected regions are ignored.
    """
    if not isinstance(region, dict):
        return None
    status, kind, action = (str(region.get(k) or "") for k in ("status", "kind", "action"))
    if status == PROPOSED:
        if not (action in (REMOVE, AS_IMAGE) or kind in (FORMULA, PICTURE)):
            return None
    elif status != CONFIRMED:
        return None
    try:
        x0, y0, x1, y1 = (round(float(v)) for v in region["bbox"])
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    text = region.get("text")
    latex = text if kind == FORMULA and isinstance(text, str) and text.strip() else None
    return RegionDecision(kind, (x0, y0, x1, y1), action or KEEP, latex)


def region_decisions(regions: Any, n_pages: int) -> list[list[RegionDecision]] | None:
    """One list of region dicts per access page -> per-page decisions; None (heuristics only) when absent."""
    if not isinstance(regions, list | tuple):
        return None
    if len(regions) != n_pages:
        raise ValueError(f"{len(regions)} region lists for {n_pages} access pages")
    return [[d for r in (page or []) if (d := decision(r)) is not None] for page in regions]
