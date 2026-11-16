"""A whole book through the real tools: recode_pdf (+ jbig2, jpegoptim), pikepdf, veraPDF, pandoc, OpenJPEG.

The image has every tool; on a laptop the test skips the ones that are missing. Run with ``pytest -m tools``
in the image (CI does) to make the skips impossible to miss.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pikepdf
import pytest

from evilflowers_render import book as book_module
from evilflowers_render.contract import BookJob
from evilflowers_render.settings import Settings
from tests.conftest import ITEM

pytestmark = pytest.mark.tools

RECODE = Path(sys.executable).parent / "recode_pdf"
MISSING = [
    tool
    for tool, found in {
        "recode_pdf": RECODE.exists() or shutil.which("recode_pdf"),
        "jbig2": shutil.which("jbig2"),
        "jpegoptim": shutil.which("jpegoptim"),
        "verapdf": shutil.which("verapdf"),
        "pandoc": shutil.which("pandoc"),
    }.items()
    if not found
]


@pytest.mark.skipif(bool(MISSING), reason=f"missing tools: {MISSING}")
def test_a_book_through_every_tool(tmp_path: Path, make_book):
    settings = Settings(storage_path=tmp_path, log_format="text", verapdf_required=True)
    message, _ = make_book(formats=("pdf", "pdfa", "epub"), jp2=True)

    summary = book_module.run(BookJob.model_validate(message), settings)

    outcome = json.loads(settings.storage_file(message["outcome"]).read_text())
    assert outcome["status"] == "done", outcome["error"]
    assert summary["files"] == ["epub", "images", "pdf", "pdfa"]
    assert outcome["pdfa_valid"] is True, "veraPDF rejected the archival master"
    out = settings.storage_file(message["output_dir"])
    with pikepdf.open(out / f"{ITEM}.pdf") as distribution:
        assert len(distribution.pages) == 3  # access leaves only
        assert distribution.is_linearized
        # the dual-output fix: JPEG (DCT) layers in the distribution copy, never JPEG 2000
        filters = {str(image.get("/Filter")) for page in distribution.pages for image in page.get_images().values()}
        assert "/JPXDecode" not in filters
        assert "/DCTDecode" in filters
    with pikepdf.open(out / f"{ITEM}_archival.pdf") as master:
        assert len(master.pages) == 3
        filters = {str(image.get("/Filter")) for page in master.pages for image in page.get_images().values()}
        assert "/JPXDecode" in filters
        with master.open_metadata() as xmp:
            assert xmp["pdfaid:part"] == "2"
            assert xmp["dc:title"] == "Skúšobná kniha"
    with zipfile.ZipFile(out / f"{ITEM}.epub") as epub:
        assert epub.namelist()[0] == "mimetype"
    with zipfile.ZipFile(out / f"{ITEM}_jp2.zip") as package:
        assert len(package.namelist()) == 4
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [f"{ITEM}.pdf", f"{ITEM}_archival.pdf", f"{ITEM}.epub", f"{ITEM}_jp2.zip", "outcome.json"]
    )
