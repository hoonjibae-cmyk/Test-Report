"""API memory safeguards: serialize heavy work and bound PDF rasterization."""
import asyncio
import base64
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import cv2
from PIL import Image
import pymupdf
import pytest
from fastapi import HTTPException, UploadFile

from omr_api import main as api


def _pdf(path, count=2, width=100, height=200):
    with pymupdf.open() as doc:
        for index in range(count):
            page = doc.new_page(width=width, height=height)
            page.insert_text((10, 20), f"Page {index + 1}")
        doc.save(path)
    return str(path)


def test_heavy_endpoints_share_one_slot(monkeypatch):
    active = 0
    peak = 0
    calls = []
    lock = threading.Lock()

    def work(name, result):
        def run(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(name)
            time.sleep(0.03)
            with lock:
                active -= 1
            return result
        return run

    monkeypatch.setattr(api, "preview_sheet", work("preview", "png"))
    monkeypatch.setattr(api, "generate_sheet", work("generate", {"pdf_base64": "pdf"}))
    monkeypatch.setattr(api, "_read_all", work("read", ([], [])))
    monkeypatch.setattr(api, "API_KEY", "")

    async def exercise():
        monkeypatch.setattr(api, "_read_slots", asyncio.Semaphore(1))
        results = await asyncio.gather(
            api.preview_endpoint(api.SheetSpec(), None),
            api.generate_endpoint(api.SheetSpec(), None),
            api.read_scans(files=[], template="{}", spec=None, threshold=0.30,
                           include_preview=False, include_essay_crops=False, x_api_key=None),
        )
        assert results == ["png", {"pdf_base64": "pdf"}, {"results": [], "problems": []}]

    asyncio.run(exercise())
    assert peak == 1
    assert sorted(calls) == ["generate", "preview", "read"]


def test_pdf_pages_are_identical_and_cleaned_one_at_a_time(tmp_path):
    path = _pdf(tmp_path / "scan.pdf")
    pages = api._expand_pdf_pages(path, str(tmp_path))
    first_number, first = next(pages)
    assert first_number == 1
    assert Path(first).exists()
    assert len(list(tmp_path.glob("omr_page_*.png"))) == 1
    # A4/normal pages retain the original 200dpi RGB raster, not a resized input.
    with pymupdf.open(path) as doc:
        reference = doc[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72),
                                      colorspace=pymupdf.csRGB, alpha=False)
        with Image.open(first) as actual:
            assert actual.size == (reference.width, reference.height)
            assert actual.tobytes() == reference.samples
    # No pixmap is retained while the caller reads this PNG.
    assert "pix" not in pages.gi_frame.f_locals
    second_number, second = next(pages)
    assert second_number == 2
    assert not Path(first).exists()
    assert Path(second).exists()
    pages.close()
    assert not Path(second).exists()


def test_pdf_page_limit_is_explicit_without_partial_processing(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "too_many.pdf", count=3)

    def forbidden(*args, **kwargs):
        pytest.fail("PDF must be rejected before rasterization")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", forbidden)
    with pytest.raises(ValueError, match="3페이지"):
        next(api._expand_pdf_pages(path, str(tmp_path), max_pages=2))
    assert not list(tmp_path.glob("omr_page_*.png"))


def test_pdf_pixel_limit_is_checked_before_any_page_is_rendered(tmp_path, monkeypatch):
    path = tmp_path / "large.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=100)
        doc.new_page(width=10000, height=10000)
        doc.save(path)

    def forbidden(*args, **kwargs):
        pytest.fail("Even the first page must wait for all dimension checks")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", forbidden)
    with pytest.raises(ValueError, match="2페이지"):
        next(api._expand_pdf_pages(str(path), str(tmp_path)))
    assert not list(tmp_path.glob("omr_page_*.png"))


def test_pdf_cleanup_on_render_failure(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "broken.pdf")

    def fail(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", fail)
    with pytest.raises(RuntimeError, match="render failed"):
        next(api._expand_pdf_pages(path, str(tmp_path)))
    assert not list(tmp_path.glob("omr_page_*.png"))


def test_read_pdf_keeps_results_and_cleans_pages(tmp_path, monkeypatch):
    path = _pdf(tmp_path / "scan.pdf")
    seen = []

    def fake_read(image_path, *args, **kwargs):
        assert Path(image_path).exists()
        assert len(list(tmp_path.glob("omr_page_*.png"))) == 1
        seen.append(image_path)
        return SimpleNamespace(
            resolved_student_id=lambda: "1234", student_id_qr="1234",
            student_id_bubbles="1234", preferred_schedule="화목", enrollment="신규생",
            exam_id="EXAM", layout_fingerprint="fingerprint", answers=lambda: {1: 2},
            selections=lambda: {1: [2]}, review_flags=[], uncertain_questions=lambda: [],
            multi_marked_questions=lambda: [], id_conflict=lambda: False,
            preview_jpeg=b"preview", essay_crops={2: b"crop"},
        )

    monkeypatch.setattr(api, "read_omr", fake_read)
    results, problems = api._read_all(
        [("scan.pdf", path)], str(tmp_path), "template.json", api.ReadParams(),
        True, True, '{"layout_fingerprint": "fingerprint"}',
    )
    assert not problems
    assert [r["filename"] for r in results] == ["scan.pdf#p1", "scan.pdf#p2"]
    assert results[0]["answers"] == {"1": 2}
    assert results[0]["selections"] == {"1": [2]}
    assert results[0]["student_id"] == "1234"
    assert results[0]["expected_layout"] == "fingerprint"
    assert base64.b64decode(results[0]["preview_jpeg_base64"]) == b"preview"
    assert base64.b64decode(results[0]["essay_crops_base64"]["2"]) == b"crop"
    assert all(not Path(p).exists() for p in seen)


@pytest.mark.parametrize("dpi", [-1, 0, 49, 301, 10000])
def test_generate_preview_rejects_excessive_dpi_before_render(monkeypatch, dpi):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid preview dpi must be rejected before rendering")

    monkeypatch.setattr(api, "generate", forbidden)
    monkeypatch.setattr(api, "API_KEY", "")
    with pytest.raises(HTTPException) as error:
        api.generate_sheet(api.SheetSpec(dpi=dpi, include_preview=True))
    assert error.value.status_code == 422


def test_api_reports_opencv_thread_limit():
    assert api.health()["version"] == "2026-11-04.1"
    assert api.health()["opencv_threads"] == api.OPENCV_THREADS
    assert cv2.getNumThreads() == api.OPENCV_THREADS


def test_cancelled_read_keeps_slot_and_temp_directory_until_worker_exits(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    first_finished = threading.Event()
    second_started = threading.Event()
    first_dir = []
    errors = []

    def slow_read(saved, work_dir, *args):
        first_dir.append(work_dir)
        started.set()
        if not release.wait(timeout=5):
            errors.append("Worker was not released")
        if not Path(work_dir).is_dir():
            errors.append("Request removed its directory before the worker finished")
        first_finished.set()
        return [], []

    def second_work(*args):
        if not first_finished.is_set():
            errors.append("Cancelled worker overlapped the next request")
        second_started.set()
        return "png"

    monkeypatch.setattr(api, "_read_all", slow_read)
    monkeypatch.setattr(api, "preview_sheet", second_work)
    monkeypatch.setattr(api, "API_KEY", "")

    async def exercise():
        monkeypatch.setattr(api, "_read_slots", asyncio.Semaphore(1))
        first = asyncio.create_task(api.read_scans(
            files=[], template="{}", spec=None, threshold=0.30,
            include_preview=False, include_essay_crops=False, x_api_key=None,
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            second = asyncio.create_task(api.preview_endpoint(api.SheetSpec(), None))
            await asyncio.sleep(0.03)
            first.cancel()  # Repeated native cancellation must not release the slot.
            await asyncio.sleep(0.03)
            assert not first.done()
            assert not second_started.is_set()
            assert Path(first_dir[0]).is_dir()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "png"
        assert not Path(first_dir[0]).exists()

    asyncio.run(exercise())
    assert not errors


def test_duplicate_upload_names_do_not_overwrite_scans_or_template(monkeypatch):
    original_template = '{"layout_fingerprint": "safe-template"}'
    uploads = [
        UploadFile(filename="scan.png", file=io.BytesIO(b"first scan")),
        UploadFile(filename="scan.png", file=io.BytesIO(b"second scan")),
        UploadFile(filename="template.json", file=io.BytesIO(b"not the template")),
    ]
    observed_paths = []

    def inspect_uploads(saved, work_dir, tpl_path, *args):
        assert [name for name, path in saved] == ["scan.png", "scan.png", "template.json"]
        paths = [path for name, path in saved]
        assert len(set(paths + [tpl_path])) == 4
        assert [Path(path).read_bytes() for path in paths] == [
            b"first scan", b"second scan", b"not the template",
        ]
        assert Path(tpl_path).read_text() == original_template
        observed_paths.extend(paths)
        return [{"filename": name} for name, path in saved], []

    monkeypatch.setattr(api, "API_KEY", "")
    monkeypatch.setattr(api, "_read_all", inspect_uploads)

    async def exercise():
        monkeypatch.setattr(api, "_read_slots", asyncio.Semaphore(1))
        return await api.read_scans(
            files=uploads, template=original_template, spec=None, threshold=0.30,
            include_preview=False, include_essay_crops=False, x_api_key=None,
        )

    result = asyncio.run(exercise())
    assert result == {"results": [{"filename": "scan.png"}, {"filename": "scan.png"},
                                   {"filename": "template.json"}], "problems": []}
    assert all(not Path(path).exists() for path in observed_paths)
