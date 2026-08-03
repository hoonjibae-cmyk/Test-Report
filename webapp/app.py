"""OMR 업무용 웹앱 — FastAPI.

실행:  uvicorn webapp.app:app --reload --port 8000
"""
from __future__ import annotations

import os
import secrets
import shutil

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from omr.generator import generate
from omr.reader import read_omr, ReadParams
from omr.batch import read_folder
from omr.scorer import score_batch, compute_grade
from omr.report_web import build_reports, ExamMeta

from . import storage

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
LOGO = os.path.join(REPO, "assets", "academy-logo.png")

# 배포용 로그인 보호: OMR_PASSWORD가 설정되면 관리 화면을 잠근다(성적표 열람은 공개).
PASSWORD = os.environ.get("OMR_PASSWORD", "")
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(16)

app = FastAPI(title="OMR 채점 시스템")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
templates.env.globals["auth_enabled"] = bool(PASSWORD)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


class AuthMiddleware(BaseHTTPMiddleware):
    """OMR_PASSWORD가 있으면 미인증 접근을 로그인으로 보낸다.

    공개 경로: /login, /static, /healthz, 성적표 열람(/reports/view/…).
    """

    async def dispatch(self, request, call_next):
        if not PASSWORD:
            return await call_next(request)
        path = request.url.path
        public = (path == "/login" or path == "/healthz"
                  or path.startswith("/static") or "/reports/view/" in path)
        if not public and not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


# add 순서 주의: 나중에 add 된 것이 바깥(먼저 실행). Session이 바깥이어야 세션 사용 가능.
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 12, same_site="lax")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": False})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if PASSWORD and secrets.compare_digest(password, PASSWORD):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": True})


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
def ensure_sheet(exam: dict) -> dict:
    """OMR PDF·미리보기·템플릿을 (없으면) 생성한다."""
    d = storage.exam_dir(exam["exam_id"])
    cfg = storage.sheet_config(exam, logo_path=LOGO if os.path.exists(LOGO) else "")
    return generate(cfg, d, dpi=200, make_preview=True)


def _key_progress(exam: dict) -> tuple[int, int]:
    n = exam["sheet"]["num_questions"]
    entered = len([v for v in exam["key"].get("answers", {}).values() if v])
    return entered, n


def _load_results(exam_id: str):
    import json
    p = os.path.join(storage.exam_dir(exam_id), "results.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fp:
            return json.load(fp)
    return None


# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"exams": storage.list_exams()})


@app.get("/exams/new", response_class=HTMLResponse)
def new_exam_form(request: Request):
    return templates.TemplateResponse(request, "new_exam.html", {})


@app.post("/exams/new")
def new_exam(
    title: str = Form(...), report_type: str = Form("basic"),
    style: str = Form("exam"), num_questions: int = Form(45),
    num_choices: int = Form(5), id_digits: int = Form(5), per_column: int = Form(20),
    period: str = Form(""), subject_label: str = Form(""),
    date: str = Form(""), school: str = Form(""), academy: str = Form("목동유쌤영어학원"),
    default_point: float = Form(2),
):
    exam = storage.create_exam(locals())
    return RedirectResponse(f"/exams/{exam['exam_id']}", status_code=303)


@app.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_page(request: Request, exam_id: str):
    exam = storage.load_exam(exam_id)
    if not exam:
        return RedirectResponse("/", status_code=303)
    entered, total = _key_progress(exam)
    results = _load_results(exam_id)
    import glob
    scans = glob.glob(os.path.join(storage.scans_dir(exam_id), "*"))
    report_manifest = None
    mpath = os.path.join(storage.reports_root(exam_id), exam_id, "manifest.json")
    if os.path.exists(mpath):
        import json
        with open(mpath, encoding="utf-8") as fp:
            report_manifest = json.load(fp)
    return templates.TemplateResponse(request, "exam.html", {
        "exam": exam, "key_entered": entered, "key_total": total,
        "results": results, "scan_count": len(scans), "manifest": report_manifest,
    })


@app.get("/exams/{exam_id}/omr.pdf")
def omr_pdf(exam_id: str):
    exam = storage.load_exam(exam_id)
    if not exam:
        return JSONResponse({"error": "not found"}, status_code=404)
    res = ensure_sheet(exam)
    return FileResponse(res["pdfs"][0], media_type="application/pdf",
                        filename=f"{exam_id}_OMR.pdf")


@app.get("/exams/{exam_id}/omr.png")
def omr_png(exam_id: str):
    exam = storage.load_exam(exam_id)
    if not exam:
        return JSONResponse({"error": "not found"}, status_code=404)
    res = ensure_sheet(exam)
    return FileResponse(res["previews"][0], media_type="image/png")


@app.post("/exams/{exam_id}/key")
async def save_key(exam_id: str, request: Request):
    exam = storage.load_exam(exam_id)
    if not exam:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    answers = {}
    for q in range(1, exam["sheet"]["num_questions"] + 1):
        v = form.get(f"q{q}")
        if v:
            answers[str(q)] = int(v)
    exam["key"]["answers"] = answers
    cuts = form.get("grade_cuts", "").strip()
    if cuts:
        try:
            exam["key"]["grade_cuts"] = [float(x) for x in cuts.replace(" ", "").split(",") if x]
        except ValueError:
            pass
    storage.save_exam(exam)
    return RedirectResponse(f"/exams/{exam_id}#key", status_code=303)


@app.post("/exams/{exam_id}/apply-standard")
def apply_standard(exam_id: str):
    exam = storage.load_exam(exam_id)
    if exam and exam["report_type"] == "english":
        storage.apply_english_standard(exam)
        storage.save_exam(exam)
    return RedirectResponse(f"/exams/{exam_id}#key", status_code=303)


@app.post("/exams/{exam_id}/scans")
async def upload_scans(exam_id: str, files: list[UploadFile] = File(...),
                       roster: UploadFile | None = File(None)):
    exam = storage.load_exam(exam_id)
    if not exam:
        return RedirectResponse("/", status_code=303)
    sdir = storage.scans_dir(exam_id)
    os.makedirs(sdir, exist_ok=True)
    for f in files:
        if not f.filename:
            continue
        with open(os.path.join(sdir, os.path.basename(f.filename)), "wb") as out:
            shutil.copyfileobj(f.file, out)

    roster_map = {}
    if roster and roster.filename:
        import json
        data = json.loads((await roster.read()).decode("utf-8"))
        rows = data if isinstance(data, list) else []
        for r in rows:
            roster_map[str(r.get("id"))] = r

    ensure_sheet(exam)  # 템플릿 보장
    records, problems = read_folder(sdir, storage.template_path(exam_id),
                                    roster={k: v.get("name", "") for k, v in roster_map.items()})
    # roster의 학교/반 병합
    for rec in records:
        info = roster_map.get(rec["student_id"], {})
        rec["school"] = info.get("school", exam.get("school", ""))
        rec["class"] = info.get("class", "")

    key = storage.answer_key(exam)
    scored = score_batch(records, key) if key else []
    grades = {}
    if key and exam["report_type"] == "english" and key.grade_cuts:
        for s in scored:
            grades[s["student_id"]] = compute_grade(s["raw_score"], key.grade_cuts)
    import json
    with open(os.path.join(storage.exam_dir(exam_id), "results.json"), "w", encoding="utf-8") as fp:
        json.dump({"records": records, "scored": scored, "problems": problems, "grades": grades},
                  fp, ensure_ascii=False, indent=2)
    return RedirectResponse(f"/exams/{exam_id}#score", status_code=303)


@app.post("/exams/{exam_id}/reports")
def make_reports(exam_id: str, base_url: str = Form("")):
    exam = storage.load_exam(exam_id)
    results = _load_results(exam_id)
    key = storage.answer_key(exam) if exam else None
    if not (exam and results and key and results["records"]):
        return RedirectResponse(f"/exams/{exam_id}#report", status_code=303)
    # JSON 직렬화로 문자열이 된 answers 키를 int로 복원(채점 정확성)
    records = []
    for rec in results["records"]:
        rec = dict(rec)
        rec["answers"] = {int(k): v for k, v in rec["answers"].items()}
        records.append(rec)
    results = {**results, "records": records}
    meta = ExamMeta(exam_id=exam_id, title=exam["title"], date=exam.get("date", ""),
                    school=exam.get("school", ""), report_type=exam["report_type"])
    build_reports(results["records"], key, meta, storage.exam_dir(exam_id),
                  base_url=base_url or f"/exams/{exam_id}/reports/view",
                  salt=f"omr-{exam_id}")
    return RedirectResponse(f"/exams/{exam_id}#report", status_code=303)


@app.get("/exams/{exam_id}/reports/view/{token}", response_class=HTMLResponse)
def view_report(exam_id: str, token: str):
    token = os.path.basename(token)
    if not token.endswith(".html"):
        token += ".html"
    p = os.path.join(storage.reports_root(exam_id), exam_id, token)
    if not os.path.exists(p):
        return HTMLResponse("<h3>성적표를 찾을 수 없습니다.</h3>", status_code=404)
    return FileResponse(p, media_type="text/html")


@app.post("/exams/{exam_id}/delete")
def delete_exam(exam_id: str):
    d = storage.exam_dir(exam_id)
    if os.path.isdir(d):
        shutil.rmtree(d)
    return RedirectResponse("/", status_code=303)
