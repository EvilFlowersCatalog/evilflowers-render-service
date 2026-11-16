"""IA file names of the media this service writes.

The subset of the digitalizer's ``studio/adapters/ia/naming.py`` (98bf9b0) that concerns media:
the orchestrator only renames these files into the item, so the spelling must match its table.
``_meta.xml``, ``_scandata.xml``, ``_files.xml``, ``_hocr.html`` and ``_djvu.txt`` are the
orchestrator's and never written here.
"""

from __future__ import annotations

#: artifact kind (the outcome's ``files`` keys) -> suffix appended to the item id
MEDIA_SUFFIX: dict[str, str] = {
    "pdf": ".pdf",
    "pdfa": "_archival.pdf",
    "epub": ".epub",
    "images": "_jp2.zip",
}

#: the formats a request may ask for (``images`` comes from ``request.jp2``, not ``formats``)
FORMATS = ("pdf", "pdfa", "epub")


def ia_name(item_id: str, kind: str) -> str:
    """The IA file name of ``kind`` for ``item_id``."""
    return f"{item_id}{MEDIA_SUFFIX[kind]}"


def leaf_image_name(item_id: str, leaf_num: int, ext: str = "jp2") -> str:
    """``<id>_jp2/<id>_0007.jp2``: the member name IA expects inside ``_jp2.zip``."""
    return f"{item_id}_{ext}/{item_id}_{leaf_num:04d}.{ext}"
