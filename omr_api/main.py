"""상태 없는(stateless) OMR API 서비스.

기존 Next.js + Supabase 앱(mock-report-web)이 호출하는 마이크로서비스.
파일을 서버에 저장하지 않고, 요청마다 처리해 결과만 돌려준다.

엔드포인트:
  GET  /health              상태 확인
  POST /generate            시트 설정(JSON) → 답안지 PDF(base64) + 판독 템플릿(JSON)
  POST /read                템플릿(JSON) + 스캔 이미지들 → 응시자별 판독 결과(JSON)

인증: 환경변수 OMR_API_KEY 가 설정되면 요청 헤더 `X-API-Key` 로 검증한다.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
from contextlib import closing

import anyio
import cv2
from fastapi.responses import Response
from fastapi import FastAPI, Form, File, UploadFile, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from omr.layout import build_exam_layout, build_layout, SheetConfig, build_layout
from omr.generator import render_sheet_image, generate
from omr.reader import read_omr, ReadParams

API_KEY = os.environ.get("OMR_API_KEY", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
LOGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "academy-logo.png")

app = FastAPI(title="OMR API", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["*"], allow_headers=["*"],
)


def _check_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


# 한 번에 이미지/PDF를 처리할 요청 수. 생성과 판독이 같은 슬롯을 쓴다.
#
# 판독은 CPU와 메모리를 함께 먹는다. 큰 스캔과 생성 이미지의 작업 버퍼가
# 겹치면 작은 요금제(512MB)에서는 프로세스가 통째로 죽을 수 있다.
# CPU도 요금제에 걸린 만큼만 나눠 쓰는 것이라, 같이 돌린다고 총 처리량이
# 늘지도 않는다. 여러 선생님이 동시에 올려도 요청 단위로 번갈아 처리된다.
#
# 서버를 키운 뒤에는 OMR_MAX_CONCURRENCY 로 올릴 수 있다.
try:
    MAX_CONCURRENCY = max(1, int(os.environ.get("OMR_MAX_CONCURRENCY", "1")))
except ValueError:
    MAX_CONCURRENCY = 1

_read_slots = asyncio.Semaphore(MAX_CONCURRENCY)

# OpenCV 내부 작업 스레드도 각각 버퍼를 잡는다. 작은 인스턴스에서는 요청을
# 직렬화해도 기본 스레드 수만으로 메모리 상한을 넘길 수 있다.
try:
    OPENCV_THREADS = max(1, int(os.environ.get("OMR_OPENCV_THREADS", "1")))
except ValueError:
    OPENCV_THREADS = 1
cv2.setNumThreads(OPENCV_THREADS)

# A4 300dpi(약 870만 픽셀)보다 여유 있게 두되, 비정상적으로 큰 PDF 페이지를
# 래스터화하기 전에 거른다. 작은 요금제에서 픽스맵 하나가 메모리를 소진할 수 있다.
MAX_PDF_PAGE_PIXELS = 12_000_000
MAX_PREVIEW_DPI = 300


async def _run_image_work(func, *args):
    async with _read_slots:
        worker = asyncio.create_task(anyio.to_thread.run_sync(func, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # asyncio.Task.cancel()로 요청이 취소되어도 실행 중인 스레드는 멈추지
            # 않는다. 작업이 끝날 때까지 슬롯과 호출자의 임시 폴더를 유지한다.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()  # 취소 중 발생한 예외도 회수한다.
            raise


class SheetSpec(BaseModel):
    exam_id: str = "EXAM"
    title: str = "모의고사"
    num_questions: int = 45
    num_choices: int = 5
    id_digits: int = 5
    per_column: int = 20
    style: str = "exam"            # "exam"(수능형) | "basic"
    period: str = ""
    subject_label: str = ""
    academy: str = "목동유쌤영어학원"
    essay_count: int = 0           # 서술형(주관식) 문항 수 — 손기입 칸
    title_suffix: bool = True      # 제목에 '답안지'가 없을 때 덧붙일지(공통 양식은 False)
    dpi: int = 200
    preferred_schedule: bool = False  # 선호 요일 칸(반배치고사)
    enrollment_mark: bool = False     # 재원 구분(재원생/신규생) 칸(반배치고사) — 좌측 패널 재배치
    include_preview: bool = False   # true면 미리보기 PNG(base64)도 반환


@app.get("/health")
def health():
    try:
        import pymupdf  # noqa: F401
        pdf_ok = True
    except Exception:
        pdf_ok = False
    return {
        "ok": True,
        "service": "omr-api",
        "auth": bool(API_KEY),
        "version": "2026-11-04.1",
        "pdf": pdf_ok,
        "max_concurrency": MAX_CONCURRENCY,
        "opencv_threads": OPENCV_THREADS,
    }


@app.post("/preview")
async def preview_endpoint(spec: SheetSpec, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return await _run_image_work(preview_sheet, spec, x_api_key)


def preview_sheet(spec: SheetSpec, x_api_key: str | None = None):
    """설정만 보고 답안지 한 장을 PNG로 그려 돌려준다 — 시험을 만들기 전 미리보기.

    /generate 와 같은 코드로 그린다. 미리보기가 실제 답안지와 조금이라도 다르면
    미리보기가 없는 것보다 나쁘므로, 배치 코드는 한 곳(omr.layout)만 쓴다.
    PDF 대신 낮은 해상도(기본 100dpi)의 PNG 한 장이라 0.1초쯤 걸린다.
    """
    _check_key(x_api_key)
    cfg = SheetConfig(
        exam_id="PREVIEW", title=spec.title, num_questions=spec.num_questions,
        num_choices=spec.num_choices, id_digits=spec.id_digits,
        questions_per_column=spec.per_column, style=spec.style,
        period=spec.period, subject_label=spec.subject_label, academy=spec.academy,
        essay_count=spec.essay_count, title_suffix=spec.title_suffix,
        preferred_schedule=spec.preferred_schedule, enrollment_mark=spec.enrollment_mark,
        academy_logo=LOGO if os.path.exists(LOGO) else "",
    )
    dpi = max(50, min(int(spec.dpi or 100), 150))
    try:
        layout = build_exam_layout(cfg) if cfg.style == "exam" else build_layout(cfg)
        img = render_sheet_image(layout, dpi=dpi)
    except ValueError as e:
        # 설정이 종이에 안 맞는 경우 — 화면이 그대로 보여 줄 수 있는 안내문
        raise HTTPException(status_code=422, detail=str(e))
    try:
        with io.BytesIO() as buf:
            img.save(buf, format="PNG", optimize=True)
            return Response(content=buf.getvalue(), media_type="image/png",
                            headers={"Cache-Control": "private, max-age=3600"})
    finally:
        img.close()

@app.post("/generate")
async def generate_endpoint(spec: SheetSpec, x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return await _run_image_work(generate_sheet, spec, x_api_key)


def generate_sheet(spec: SheetSpec, x_api_key: str | None = None):
    """답안지 PDF + 판독 템플릿을 생성해 반환한다(디스크 미저장)."""
    _check_key(x_api_key)
    if spec.include_preview and not 50 <= spec.dpi <= MAX_PREVIEW_DPI:
        raise HTTPException(status_code=422,
                            detail=f"미리보기 해상도는 50~{MAX_PREVIEW_DPI}dpi 범위여야 합니다.")
    cfg = SheetConfig(
        exam_id=spec.exam_id, title=spec.title, num_questions=spec.num_questions,
        num_choices=spec.num_choices, id_digits=spec.id_digits,
        questions_per_column=spec.per_column, style=spec.style,
        period=spec.period, subject_label=spec.subject_label, academy=spec.academy,
        essay_count=spec.essay_count, title_suffix=spec.title_suffix,
        preferred_schedule=spec.preferred_schedule, enrollment_mark=spec.enrollment_mark,
        academy_logo=LOGO if os.path.exists(LOGO) else "",
    )
    with tempfile.TemporaryDirectory() as d:
        try:
            res = generate(cfg, d, dpi=spec.dpi, make_preview=spec.include_preview)
        except ValueError as e:
            # 설정이 종이에 안 맞는 경우 — 호출 측이 그대로 보여줄 수 있는 안내문이다.
            raise HTTPException(status_code=422, detail=str(e))
        with open(res["template"], encoding="utf-8") as fp:
            template = json.load(fp)
        with open(res["pdfs"][0], "rb") as fp:
            pdf_b64 = base64.b64encode(fp.read()).decode("ascii")
        out = {"template": template, "pdf_base64": pdf_b64,
               "filename": f"{spec.exam_id}_OMR.pdf"}
        if spec.include_preview and res["previews"]:
            with open(res["previews"][0], "rb") as fp:
                out["preview_png_base64"] = base64.b64encode(fp.read()).decode("ascii")
    return out


@app.post("/read")
async def read_scans(
    files: list[UploadFile] = File(...),             # 스캔 이미지들
    template: str | None = Form(default=None),       # 판독 템플릿 JSON(문자열)
    spec: str | None = Form(default=None),           # 또는 시트 설정(JSON) → 템플릿 자동 생성
    threshold: float = Form(0.30),
    include_preview: bool = Form(False),   # 검수 화면용 미리보기(JPEG base64) 동봉
    include_essay_crops: bool = Form(False),  # 주관식 손기입 칸 이미지(전사용) 동봉
    x_api_key: str | None = Header(default=None),
):
    """스캔 이미지들을 판독해 응시자별 답안을 반환한다.

    `template`(판독 템플릿) 또는 `spec`(시트 설정) 중 하나를 주면 된다.
    spec을 주면 서버가 동일 설정으로 템플릿을 재생성하므로, 호출 측은 작은 설정만
    저장하면 된다(생성 시 쓴 설정과 같아야 함).

    반환: {results:[{filename, student_id, answers{문항:보기|null},
                     selections{문항:[보기,...]}, review_flags,
                     student_id_qr, student_id_bubbles, exam_id}], problems:[...]}
    `answers`는 단일 선택 기준(중복 표기는 null), `selections`는 칠해진 보기 전부다.
    """
    _check_key(x_api_key)
    if not template and not spec:
        raise HTTPException(status_code=422, detail="template 또는 spec 중 하나가 필요합니다.")
    if not template:
        s = SheetSpec(**json.loads(spec))
        cfg = SheetConfig(
            exam_id=s.exam_id, title=s.title, num_questions=s.num_questions,
            num_choices=s.num_choices, id_digits=s.id_digits,
            questions_per_column=s.per_column, style=s.style,
            period=s.period, subject_label=s.subject_label, academy=s.academy,
            essay_count=s.essay_count, title_suffix=s.title_suffix,
            preferred_schedule=s.preferred_schedule, enrollment_mark=s.enrollment_mark,
        )
        try:
            template = json.dumps(build_layout(cfg).template_dict(s.dpi), ensure_ascii=False)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    params = ReadParams(mark_abs_min=threshold)
    with tempfile.TemporaryDirectory() as d:
        tpl_path = os.path.join(d, "template.json")
        with open(tpl_path, "w", encoding="utf-8") as fp:
            fp.write(template)

        # 업로드를 먼저 디스크로 내린다. 통째로 메모리에 올리면 60쪽짜리 PDF
        # 하나에 수십 MB가 잡혀, 판독에 써야 할 몫을 미리 깎아먹는다.
        saved: list[tuple[str, str]] = []
        for f in files:
            if not f.filename:
                continue
            # 같은 이름의 스캔도 별개 파일이다. 업로드 이름으로 저장하면 앞선
            # 스캔이나 template.json을 덮어쓸 수 있으므로 임의 경로를 쓴다.
            suffix = os.path.splitext(os.path.basename(f.filename))[1]
            fd, raw_path = tempfile.mkstemp(prefix="omr_upload_", suffix=suffix, dir=d)
            with os.fdopen(fd, "wb") as out:
                while chunk := await f.read(1024 * 1024):
                    out.write(chunk)
            saved.append((f.filename, raw_path))

        # 판독은 CPU를 오래 붙잡는 작업이라 이벤트 루프에서 돌리면 안 된다.
        # 그 사이 서버는 /health 에도 답하지 못하고, 배포 플랫폼은 그것을
        # '죽은 서버'로 읽어 판독 도중에 인스턴스를 재시작해 버린다.
        results, problems = await _run_image_work(
            _read_all, saved, d, tpl_path, params,
            include_preview, include_essay_crops, template,
        )
    return {"results": results, "problems": problems}


def _read_all(saved, work_dir, tpl_path, params, include_preview,
              include_essay_crops, template):
    """올려받은 파일들을 차례로 판독한다.

    작업 스레드에서 돌린다. 이벤트 루프를 붙잡지 않는 것이 목적이므로,
    여기서는 파일 읽기와 판독만 하고 네트워크는 건드리지 않는다.
    """
    expected_layout = json.loads(template).get("layout_fingerprint")
    results, problems = [], []
    def read_target(name, img_path):
        try:
            r = read_omr(img_path, tpl_path, params=params,
                         make_preview=include_preview,
                         make_essay_crops=include_essay_crops)
        except Exception as e:
            problems.append({"filename": name, "error": str(e)})
            return
        results.append({
            "filename": name,
            "student_id": r.resolved_student_id(),
            "student_id_qr": r.student_id_qr,
            "student_id_bubbles": r.student_id_bubbles,
            "preferred_schedule": r.preferred_schedule,
            "enrollment": r.enrollment,
            "exam_id": r.exam_id,
            # 답안지 QR의 레이아웃 지문 vs 이번 판독에 쓴 설정의 지문
            "sheet_layout": r.layout_fingerprint,
            "expected_layout": expected_layout,
            "answers": {str(q): v for q, v in r.answers().items()},
            # '모두 고르기' 문항 대응: 칠해진 보기를 전부 담는다.
            "selections": {str(q): v for q, v in r.selections().items()},
            "review_flags": r.review_flags,
            # 자동 검수 통과 판단용 — 경계에 걸친 것만 추린다.
            # (확실한 미표기는 여기 들어가지 않는다: 학생이 안 푼 문항이다)
            "uncertain_questions": r.uncertain_questions(),
            "multi_marked_questions": r.multi_marked_questions(),
            "id_uncertain": any(
                flag.get("type") == "id" for flag in r.review_flags
            ),
            "id_conflict": r.id_conflict(),
        })
        if include_preview and r.preview_jpeg:
            # 판독기가 실제로 본 이미지(원근 보정 + 판정 표시)
            results[-1]["preview_jpeg_base64"] = base64.b64encode(
                r.preview_jpeg).decode("ascii")
        if include_essay_crops and r.essay_crops:
            # 주관식 손기입 칸을 반듯하게 편 이미지 — 전사(글자 읽기)에 쓴다
            results[-1]["essay_crops_base64"] = {
                str(num): base64.b64encode(jpg).decode("ascii")
                for num, jpg in r.essay_crops.items()
            }

    for filename, raw_path in saved:
        if not filename.lower().endswith(".pdf"):
            read_target(filename, raw_path)
            continue
        # PDF 한 페이지만 펼친 뒤 바로 판독한다. 다음 장을 만들기 전에 PNG와
        # 픽스맵을 해제하여 페이지 수만큼 이미지가 쌓이지 않게 한다.
        try:
            with closing(_expand_pdf_pages(raw_path, work_dir)) as pages:
                for page_number, img_path in pages:
                    read_target(f"{filename}#p{page_number}", img_path)
        except Exception as e:
            problems.append({"filename": filename, "error": f"PDF를 처리하지 못했습니다: {e}"})
    return results, problems


def _expand_pdf_pages(pdf_path: str, out_dir: str, dpi: int = 200, max_pages: int = 100):
    """PDF 페이지를 하나씩 펼친다. 각 PNG는 다음 페이지로 넘어갈 때 지운다."""
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        if not doc.page_count:
            raise ValueError("PDF에 페이지가 없습니다.")
        if doc.page_count > max_pages:
            raise ValueError(
                f"PDF는 한 번에 {max_pages}페이지까지 처리할 수 있습니다. "
                f"현재 {doc.page_count}페이지이므로 파일을 나누어 올려 주세요."
            )
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        # 모든 페이지를 먼저 확인한다. 뒤쪽의 비정상 페이지 때문에 앞쪽 답안만
        # 처리된 정상 응답처럼 보이거나, 거대한 픽스맵이 생성되는 일을 막는다.
        for page in doc:
            bounds = (page.rect * matrix).irect
            if (bounds.width <= 0 or bounds.height <= 0 or
                    bounds.width * bounds.height > MAX_PDF_PAGE_PIXELS):
                raise ValueError(
                    f"PDF {page.number + 1}페이지의 이미지 크기가 너무 큽니다. "
                    "A4 크기로 저장한 PDF를 올려 주세요."
                )
        for i in range(doc.page_count):
            fd, page_path = tempfile.mkstemp(prefix="omr_page_", suffix=".png", dir=out_dir)
            os.close(fd)
            try:
                pix = doc[i].get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB,
                                        alpha=False)
                try:
                    pix.save(page_path)
                finally:
                    # yield 동안 픽스맵을 잡고 있으면 판독 이미지와 겹쳐 존재한다.
                    del pix
                    pymupdf.TOOLS.store_shrink(100)
                yield i + 1, page_path
            finally:
                if os.path.exists(page_path):
                    os.remove(page_path)
