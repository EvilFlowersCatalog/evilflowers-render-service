import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from evilflowers_render.settings import Settings

ITEM = "stu-fei-test-0001"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(storage_path=tmp_path, log_format="text", verapdf_required=False)


def page_image(path: Path, text: str = "Kapitola 1", size: tuple[int, int] = (850, 1100)) -> Path:
    """A small grey page with dark text-like bars (300 dpi letter / 3)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    draw.text((80, 80), text, fill=0)
    for row in range(200, size[1] - 150, 40):
        draw.rectangle((80, row, size[0] - 80, row + 14), fill=40)
    image.save(path, dpi=(300, 300))
    return path


def hocr_document(n_pages: int, size: tuple[int, int] = (850, 1100)) -> str:
    """A minimal multi-page hOCR (one ocr_page per access leaf, IA ids)."""
    w, h = size
    pages = []
    for i in range(n_pages):
        lines = []
        for j, row in enumerate(range(200, h - 150, 40)):
            lines.append(
                f'<span class="ocr_line" id="line_{i}_{j}" title="bbox 80 {row} {w - 80} {row + 14}; x_size 14">'
                f'<span class="ocrx_word" id="word_{i}_{j}" title="bbox 80 {row} 300 {row + 14}; x_wconf 95">'
                f"slovo{j} text riadku</span></span>"
            )
        lines.append(
            f'<span class="ocr_line" id="num_{i}" title="bbox 400 {h - 60} 430 {h - 40}; x_size 14">'
            f'<span class="ocrx_word" title="bbox 400 {h - 60} 430 {h - 40}; x_wconf 95">{i + 1}</span></span>'
        )
        pages.append(
            f'<div class="ocr_page" id="page_{i:06d}" title="bbox 0 0 {w} {h}; ppageno {i}">'
            f'<div class="ocr_carea"><p class="ocr_par">{"".join(lines)}</p></div></div>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<meta name="ocr-system" content="tesseract"/></head><body>' + "".join(pages) + "</body></html>"
    )


@pytest.fixture
def make_book(tmp_path: Path):
    """A book on the storage: 3 access leaves + 1 excluded, the request and the message."""

    def make(formats=("pdf", "pdfa"), jp2=True, regions=None, **request_extra):
        item_dir = tmp_path / ITEM
        pages = [page_image(item_dir / "digitized" / f"{i:04d}.tif", f"Strana {i}") for i in range(4)]
        access = [pages[0], pages[1], pages[3]]
        hocr = item_dir / f"{ITEM}_studio" / "render" / "access_hocr-7.html"
        hocr.parent.mkdir(parents=True, exist_ok=True)
        hocr.write_text(hocr_document(len(access)), encoding="utf-8")
        rel = lambda p: p.relative_to(tmp_path).as_posix()  # noqa: E731
        request = {
            "item_id": ITEM,
            "formats": list(formats),
            "leaves": [0, 1, 3],
            "n_leaves": 4,
            "pages": [rel(p) for p in access],
            "hocr": rel(hocr),
            "metadata": {
                "identifier": ITEM,
                "title": "Skúšobná kniha",
                "creator": ["Novák, Ján"],
                "language": ["slo"],
                "date": "1987",
                "ppi": 300,
            },
            "dpi": 300,
            "regions": regions,
            "jp2": [rel(p) for p in pages] if jp2 else [],
            **request_extra,
        }
        request_path = hocr.parent / "request-7.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        output_dir = f"{ITEM}/{ITEM}_studio/jobs/31-a1"
        message = {
            "run_id": "7",
            "job": "render-book-0000-a1",
            "token": "tok-123",
            "inputs": [rel(request_path)],
            "output_dir": output_dir,
            "outcome": f"{output_dir}/outcome.json",
            "request": rel(request_path),
        }
        return message, request

    return make
