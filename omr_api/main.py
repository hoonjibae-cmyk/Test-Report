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

import base64
import json
import os
import tempfile

from fastapi import FastAPI, Form, File, UploadFile, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from omr.layout import SheetConfig, build_layout
from omr.generator import generate
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
    dpi: int = 200
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
        "version": "2026-08-28.1",
        "pdf": pdf_ok,
    }


@app.post("/generate")
def generate_sheet(spec: SheetSpec, x_api_key: str | None = Header(default=None)):
    """답안지 PDF + 판독 템플릿을 생성해 반환한다(디스크 미저장)."""
    _check_key(x_api_key)
    cfg = SheetConfig(
        exam_id=spec.exam_id, title=spec.title, num_questions=spec.num_questions,
        num_choices=spec.num_choices, id_digits=spec.id_digits,
        questions_per_column=spec.per_column, style=spec.style,
        period=spec.period, subject_label=spec.subject_label, academy=spec.academy,
        essay_count=spec.essay_count,
        academy_logo=LOGO if os.path.exists(LOGO) else "",
    )
    with tempfile.TemporaryDirectory() as d:
        res = generate(cfg, d, dpi=spec.dpi, make_preview=spec.include_preview)
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
    x_api_key: str | None = Header(default=None),
):
    """스캔 이미지들을 판독해 응시자별 답안을 반환한다.

    `template`(판독 템플릿) 또는 `spec`(시트 설정) 중 하나를 주면 된다.
    spec을 주면 서버가 동일 설정으로 템플릿을 재생성하므로, 호출 측은 작은 설정만
    저장하면 된다(생성 시 쓴 설정과 같아야 함).

    반환: {results:[{filename, student_id, answers{문항:보기|null}, review_flags,
                     student_id_qr, student_id_bubbles, exam_id}], problems:[...]}
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
            essay_count=s.essay_count,
        )
        template = json.dumps(build_layout(cfg).template_dict(s.dpi), ensure_ascii=False)

    params = ReadParams(mark_abs_min=threshold)
    results, problems = [], []
    with tempfile.TemporaryDirectory() as d:
        tpl_path = os.path.join(d, "template.json")
        with open(tpl_path, "w", encoding="utf-8") as fp:
            fp.write(template)
        for f in files:
            if not f.filename:
                continue
            raw_path = os.path.join(d, os.path.basename(f.filename))
            with open(raw_path, "wb") as out:
                out.write(await f.read())

            # PDF는 페이지별 이미지로 풀어 각 페이지를 답안지 1장으로 판독한다.
            # 페이지 파일명은 "원본.pdf#p1" 형식 — 호출 측이 원본과 매핑할 수 있다.
            if f.filename.lower().endswith(".pdf"):
                try:
                    pages = _expand_pdf_pages(raw_path, d)
                except Exception as e:
                    problems.append({"filename": f.filename, "error": f"PDF를 열지 못했습니다: {e}"})
                    continue
                if not pages:
                    problems.append({"filename": f.filename, "error": "PDF에 페이지가 없습니다."})
                    continue
                targets = [(f"{f.filename}#p{i + 1}", p) for i, (_, p) in enumerate(pages)]
            else:
                targets = [(f.filename, raw_path)]

            for name, img_path in targets:
                try:
                    r = read_omr(img_path, tpl_path, params=params)
                except Exception as e:
                    problems.append({"filename": name, "error": str(e)})
                    continue
                results.append({
                    "filename": name,
                    "student_id": r.resolved_student_id(),
                    "student_id_qr": r.student_id_qr,
                    "student_id_bubbles": r.student_id_bubbles,
                    "exam_id": r.exam_id,
                    "answers": {str(q): v for q, v in r.answers().items()},
                    "review_flags": r.review_flags,
                })
    return {"results": results, "problems": problems}


def _expand_pdf_pages(pdf_path: str, out_dir: str, dpi: int = 200, max_pages: int = 100):
    """PDF의 각 페이지를 판독용 PNG로 렌더링해 (페이지번호, 경로) 목록을 반환."""
    import pymupdf

    pages = []
    with pymupdf.open(pdf_path) as doc:
        count = min(doc.page_count, max_pages)
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        for i in range(count):
            pix = doc[i].get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB)
            page_path = os.path.join(out_dir, f"{base}__page{i + 1}.png")
            pix.save(page_path)
            pages.append((i + 1, page_path))
    return pages
