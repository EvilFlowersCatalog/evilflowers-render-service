"""The request's ``metadata`` (the item record, IA field names) -> what the PDF XMP and the EPUB read.

Ported from evilflowers-books-digitalizer (98bf9b0): ``StudioRenderer.classic_metadata`` and the
catalog's ``normalize_language``, so the PDF XMP, the EPUB ``dc:language`` and the catalog entry agree.
Only the fields the media use are read; every other field of the record is ignored.
"""

from __future__ import annotations

from typing import Any

#: ISO 639-2 (bibliographic and terminological) -> ISO 639-1 for the collection's languages
_ALPHA3_TO_ALPHA2 = {
    "slk": "sk", "slo": "sk",
    "ces": "cs", "cze": "cs",
    "eng": "en",
    "deu": "de", "ger": "de",
    "rus": "ru",
    "hun": "hu",
    "pol": "pl",
    "ukr": "uk",
    "fra": "fr", "fre": "fr",
    "ita": "it",
    "spa": "es",
}  # fmt: skip


def normalize_language(code: str | None) -> str | None:
    """ISO 639-1 where one exists, else the alpha3 as given (``slk+eng`` -> ``sk``, ``sk-SK`` -> ``sk``)."""
    if not code:
        return None
    primary = str(code).strip().lower().split("+")[0].replace("_", "-").split("-")[0].strip()
    if not primary:
        return None
    if len(primary) == 2:
        return primary
    return _ALPHA3_TO_ALPHA2.get(primary, primary)


def _list(value: Any) -> list[str]:
    """IA repeatable fields arrive as lists; tolerate a scalar."""
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return [str(value)]


def classic_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """The item record -> the keys the PDF and EPUB code read (``classic_metadata(ItemMetadata)``)."""
    languages = _list(record.get("language"))
    lang = languages[0] if languages else ""
    date = str(record.get("date") or "")
    isbn = _list(record.get("isbn"))
    return {
        "title": str(record.get("title") or ""),
        "authors": _list(record.get("creator")),
        "language": normalize_language(lang) or lang,
        "publisher": str(record.get("publisher") or ""),
        "year": date[:4] if date else "",
        "isbn": isbn[0] if isbn else "",
        "description": " ".join(_list(record.get("description"))),
    }
