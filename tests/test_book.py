"""The job around the tools: contract, paths, outcome, failures, deadline. The tools themselves are stubbed;
``test_tools.py`` runs them for real."""

import hashlib
import json
import zipfile
from pathlib import Path

import pikepdf
import pytest

from evilflowers_render import book, pdf
from evilflowers_render.contract import BookJob
from evilflowers_render.deadline import Deadline, DeadlineExceeded
from tests.conftest import ITEM


def fake_recode(profile, pages, hocr, out, *, dpi, meta, deadline, recode=None):
    out.parent.mkdir(parents=True, exist_ok=True)
    document = pikepdf.new()
    for _ in pages:
        document.add_blank_page(page_size=(612, 792))
    with document.open_metadata() as xmp:
        xmp["pdfaid:part"] = "3"
        xmp["pdfaid:conformance"] = "B"
        xmp["dc:title"] = meta["title"]
    document.save(out)
    return out


@pytest.fixture
def stub_tools(monkeypatch):
    calls = {"recode": [], "verapdf": [], "epub": []}

    def recode(profile, pages, hocr, out, **kwargs):
        calls["recode"].append((profile.name, [p.name for p in pages], kwargs["dpi"], kwargs["meta"]))
        return fake_recode(profile, pages, hocr, out, **kwargs)

    def validate(path, **kwargs):
        calls["verapdf"].append(path.name)
        return True

    def epub(request, pages, hocr, work, target, meta, deadline):
        calls["epub"].append((len(pages), request.regions))
        target.write_bytes(b"PK epub")
        return target

    monkeypatch.setattr(pdf, "recode", recode)
    monkeypatch.setattr(pdf, "validate_pdfa", validate)
    monkeypatch.setattr(book, "render_epub", epub)
    return calls


def read_outcome(settings, message):
    return json.loads(settings.storage_file(message["outcome"]).read_text())


def test_done_outcome_lists_every_medium_with_size_and_md5(settings, make_book,stub_tools):
    message, _request = make_book(formats=("pdf", "pdfa", "epub"))
    summary = book_run(settings, message)

    outcome = read_outcome(settings, message)
    assert outcome["status"] == "done" and outcome["error"] is None
    assert outcome["run_id"] == "7" and outcome["token"] == "tok-123"
    assert outcome["pdfa_valid"] is True
    assert set(outcome["files"]) == {"pdf", "pdfa", "epub", "images"}
    out_dir = f"{ITEM}/{ITEM}_studio/jobs/31-a1"
    expected = {
        "pdf": f"{ITEM}.pdf",
        "pdfa": f"{ITEM}_archival.pdf",
        "epub": f"{ITEM}.epub",
        "images": f"{ITEM}_jp2.zip",
    }
    for kind, name in expected.items():
        entry = outcome["files"][kind]
        assert entry["path"] == f"{out_dir}/{name}"
        data = settings.storage_file(entry["path"]).read_bytes()
        assert entry["size"] == len(data)
        assert entry["md5"] == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert summary == {"job": "render-book-0000-a1", "status": "done", "files": sorted(expected), "pdfa_valid": True}
    # access leaves only, staged as 0000.tif … in a private dir that is gone afterwards
    assert stub_tools["recode"][0][:3] == ("distribution", ["0000.tif", "0001.tif", "0002.tif"], 300)
    assert stub_tools["recode"][1][0] == "archival"
    assert stub_tools["recode"][0][3]["language"] == "sk"
    assert not any(p.name.startswith(".render-") for p in settings.storage_file(out_dir).iterdir())


def test_only_media_are_written(settings, make_book,stub_tools):
    message, _ = make_book(formats=("pdf",), jp2=False)
    book_run(settings, message)
    names = {p.name for p in settings.storage_file(message["output_dir"]).iterdir()}
    assert names == {f"{ITEM}.pdf", "outcome.json"}
    outcome = read_outcome(settings, message)
    assert set(outcome["files"]) == {"pdf"}
    assert outcome["pdfa_valid"] is None  # pdfa not requested
    assert stub_tools["verapdf"] == []


def test_archival_master_is_declared_pdfa_2b_and_distribution_is_linearized(settings, make_book,stub_tools):
    message, _ = make_book(formats=("pdf", "pdfa"), jp2=False)
    book_run(settings, message)
    out = settings.storage_file(message["output_dir"])
    with pikepdf.open(out / f"{ITEM}_archival.pdf") as master, master.open_metadata() as xmp:
        assert (xmp["pdfaid:part"], xmp["pdfaid:conformance"]) == ("2", "B")
    with pikepdf.open(out / f"{ITEM}.pdf") as distribution:
        assert distribution.is_linearized
        # page labels from the printed numbers 1..3 at the page foot
        assert "/PageLabels" in distribution.Root


def test_jp2_package_holds_every_leaf_in_leaf_order(settings, make_book,stub_tools):
    message, _ = make_book(formats=("pdf",), jp2=True)
    book_run(settings, message)
    package = settings.storage_file(message["output_dir"]) / f"{ITEM}_jp2.zip"
    with zipfile.ZipFile(package) as zf:
        assert zf.namelist() == [f"{ITEM}_jp2/{ITEM}_{i:04d}.jp2" for i in range(4)]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist())
        assert zf.read(zf.namelist()[0])[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n"


def test_regions_reach_the_epub(settings, make_book,stub_tools):
    regions = [[{"id": 17, "kind": "formula", "bbox": [1, 2, 30, 40], "status": "confirmed", "action": "reocr",
                 "text": "E = mc^2"}], [], []]  # fmt: skip
    message, _ = make_book(formats=("epub",), jp2=False, regions=regions)
    book_run(settings, message)
    assert stub_tools["epub"] == [(3, [[r for r in regions[0]], [], []])]


def test_a_tool_failure_writes_failed_then_raises(settings, make_book,stub_tools, monkeypatch):
    def broken(*args, **kwargs):
        raise pdf.RenderError("recode_pdf (distribution) failed (exit 1): boom")

    monkeypatch.setattr(pdf, "recode", broken)
    message, _ = make_book(formats=("pdf",))
    with pytest.raises(book.RenderFailed, match="boom"):
        book_run(settings, message)
    outcome = read_outcome(settings, message)
    assert outcome == {
        "run_id": "7",
        "token": "tok-123",
        "status": "failed",
        "error": "recode_pdf (distribution) failed (exit 1): boom",
        "pdfa_valid": None,
        "files": {},
    }


def test_invalid_pdfa_is_reported_not_hidden(settings, make_book,stub_tools, monkeypatch):
    monkeypatch.setattr(pdf, "validate_pdfa", lambda path, **kwargs: False)
    message, _ = make_book(formats=("pdfa",), jp2=False)
    book_run(settings, message)
    assert read_outcome(settings, message)["pdfa_valid"] is False


def test_deadline_fails_the_job_with_a_time_limit_error(settings, make_book,stub_tools, monkeypatch):
    def slow(*args, deadline, **kwargs):
        raise DeadlineExceeded("recode_pdf (distribution) passed the book's deadline")

    monkeypatch.setattr(pdf, "recode", slow)
    message, _ = make_book(formats=("pdf",))
    with pytest.raises(book.RenderFailed, match="time limit exceeded"):
        book_run(settings, message)
    assert read_outcome(settings, message)["status"] == "failed"


def test_a_deliberate_stop_writes_no_outcome(settings, make_book,stub_tools, monkeypatch):
    def stopped(*args, **kwargs):
        raise SystemExit(1)  # the kit's cold cancel

    monkeypatch.setattr(pdf, "recode", stopped)
    message, _ = make_book(formats=("pdf",))
    with pytest.raises(SystemExit):
        book_run(settings, message)
    assert not settings.storage_file(message["outcome"]).exists()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"pages": ["../../etc/passwd"]}, "escapes STORAGE_PATH"),
        ({"hocr": "/etc/passwd"}, "must be relative"),
        ({"pages": ["missing/0000.tif"]}, "does not exist"),
        ({"formats": ["pdf", "djvu"]}, "breaks the contract"),
        ({"formats": ["pdf", "pdf"]}, "repeat"),
        ({"regions": [[]]}, "region lists"),
    ],
)
def test_a_request_that_breaks_the_contract_fails_the_job(settings, make_book,stub_tools, change, error):
    message, request = make_book(formats=("pdf",))
    path = settings.storage_file(message["request"])
    path.write_text(json.dumps({**request, **change}))
    with pytest.raises(book.RenderFailed, match=error):
        book_run(settings, message)
    assert read_outcome(settings, message)["status"] == "failed"
    assert stub_tools["recode"] == []


def test_the_request_must_be_an_input(settings, make_book,stub_tools):
    message, _ = make_book(formats=("pdf",))
    message["inputs"] = [message["request"].replace("request-7", "other")]
    with pytest.raises(book.RenderFailed, match="not among the job's inputs"):
        book_run(settings, message)


def test_give_up_writes_failed_and_raises(settings, make_book):
    message, _ = make_book(formats=("pdf",))
    with pytest.raises(book.GaveUp, match="gave up after 5 deliveries"):
        book.give_up(BookJob.model_validate(message), 6, settings)
    assert read_outcome(settings, message)["status"] == "failed"


def test_deadline_counts_down():
    now = [100.0]
    deadline = Deadline(10, clock=lambda: now[0])
    assert deadline.remaining() == 10
    now[0] = 109.5
    assert deadline.remaining() == 1.0  # never below 1 s
    deadline.check()
    now[0] = 111
    with pytest.raises(DeadlineExceeded):
        deadline.check()


def book_run(settings, message):
    return book.run(BookJob.model_validate(message), settings)


def test_settings_keep_the_hard_limit_under_the_visibility_timeout(tmp_path: Path):
    from evilflowers_render.settings import Settings

    with pytest.raises(ValueError, match="BROKER_VISIBILITY_TIMEOUT"):
        Settings(storage_path=tmp_path, task_soft_time_limit=40_000, task_time_limit=50_000)
