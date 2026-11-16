"""``<id>_jp2.zip``: the cleaned page images of *all* leaves as JPEG 2000 (IA's page package).

Ported from evilflowers-books-digitalizer ``studio/adapters/ia/images.py`` (98bf9b0); same options,
same member names and order, ``ZIP_STORED``. The encoder cache is left out (a job's output dir is
new every attempt, so it would never hit). New: ``check`` runs before every leaf (the deadline).

Encoding uses Pillow's OpenJPEG plugin: no Kakadu. Without OpenJPEG there is no fallback, because
IA derives from nothing else under this name.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, features

from evilflowers_render.naming import leaf_image_name


class Jp2Unsupported(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Jp2Options:
    #: compression ratio per quality layer (IA's own derive uses ~10-20 for page images)
    rates: tuple[float, ...] = (16.0,)
    irreversible: bool = True


def jp2_supported() -> bool:
    return bool(features.check("jpg_2000"))


def encode_jp2(image: Path, opts: Jp2Options = Jp2Options()) -> bytes:
    """One page as JP2 bytes; bitonal/palette pages are widened to 8 bits (OpenJPEG has no 1-bit mode)."""
    with Image.open(image) as im:
        if im.mode not in ("L", "RGB"):
            im = im.convert("RGB" if im.mode in ("P", "RGBA", "CMYK") else "L")
        buf = io.BytesIO()
        im.save(buf, "JPEG2000", quality_mode="rates", quality_layers=list(opts.rates), irreversible=opts.irreversible)
        return buf.getvalue()


def build_jp2_zip(
    item_id: str,
    images: Sequence[Path],
    out: Path,
    opts: Jp2Options = Jp2Options(),
    *,
    check: Callable[[], None] | None = None,
) -> Path:
    """Write the package atomically (``.zip.part`` then rename); ``images[i]`` is leaf ``i``."""
    if not jp2_supported():
        raise Jp2Unsupported("Pillow has no JPEG 2000 codec (features.check('jpg_2000') is False)")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
            for leaf_num, image in enumerate(images):
                if check is not None:
                    check()
                zf.writestr(leaf_image_name(item_id, leaf_num), encode_jp2(image, opts))
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out
