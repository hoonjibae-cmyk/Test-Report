"""시험 설정·산출물의 파일 기반 저장소.

data/exams/<exam_id>/
  exam.json            시험 설정(시트/정답키/메타)
  <exam_id>_template.json  판독용 템플릿(OMR 생성 시 기록)
  <exam_id>_blank.pdf  빈 답안지
  scans/               업로드된 스캔 이미지
  results.json         판독·채점 결과(레코드)
  reports/<exam_id>/   생성된 웹 성적표
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from datetime import datetime

from omr.layout import SheetConfig
from omr.scorer import AnswerKey, CATEGORY_MAP

DATA_ROOT = os.environ.get("OMR_DATA_DIR", os.path.join(os.getcwd(), "data", "exams"))


def _slugify(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣]+", "-", text).strip("-")
    return s[:40] or "EXAM"


def exam_dir(exam_id: str) -> str:
    return os.path.join(DATA_ROOT, exam_id)


def scans_dir(exam_id: str) -> str:
    return os.path.join(exam_dir(exam_id), "scans")


def reports_root(exam_id: str) -> str:
    return os.path.join(exam_dir(exam_id), "reports")


def template_path(exam_id: str) -> str:
    return os.path.join(exam_dir(exam_id), f"{exam_id}_template.json")


def list_exams() -> list[dict]:
    if not os.path.isdir(DATA_ROOT):
        return []
    out = []
    for name in os.listdir(DATA_ROOT):
        p = os.path.join(DATA_ROOT, name, "exam.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fp:
                out.append(json.load(fp))
    out.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return out


def load_exam(exam_id: str) -> dict | None:
    p = os.path.join(exam_dir(exam_id), "exam.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fp:
        return json.load(fp)


def save_exam(exam: dict) -> None:
    d = exam_dir(exam["exam_id"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "exam.json"), "w", encoding="utf-8") as fp:
        json.dump(exam, fp, ensure_ascii=False, indent=2)


def create_exam(form: dict) -> dict:
    """생성 폼 → 시험 설정 dict."""
    title = form.get("title", "").strip() or "무제 시험"
    exam_id = form.get("exam_id", "").strip() or _slugify(title) + "-" + datetime.now().strftime("%m%d%H%M")
    report_type = form.get("report_type", "basic")
    num_q = int(form.get("num_questions", 45))
    exam = {
        "exam_id": exam_id,
        "title": title,
        "date": form.get("date", ""),
        "school": form.get("school", ""),
        "academy": form.get("academy", "목동유쌤영어학원"),
        "report_type": report_type,
        "sheet": {
            "style": form.get("style", "exam"),
            "num_questions": num_q,
            "num_choices": int(form.get("num_choices", 5)),
            "id_digits": int(form.get("id_digits", 5)),
            "per_column": int(form.get("per_column", 20)),
            "period": form.get("period", ""),
            "subject_label": form.get("subject_label", ""),
        },
        "key": {
            "default_point": float(form.get("default_point", 2)),
            "grade_cuts": [],
            "answers": {},
            "points": {},
            "question_meta": {},
        },
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if report_type == "english":
        apply_english_standard(exam)
    save_exam(exam)
    return exam


def sheet_config(exam: dict, logo_path: str = "") -> SheetConfig:
    s = exam["sheet"]
    return SheetConfig(
        exam_id=exam["exam_id"], title=exam["title"],
        num_questions=s["num_questions"], num_choices=s["num_choices"],
        id_digits=s["id_digits"], questions_per_column=s["per_column"],
        style=s.get("style", "exam"), period=s.get("period", ""),
        subject_label=s.get("subject_label", ""), academy=exam.get("academy", ""),
        academy_logo=logo_path,
    )


def write_answer_key_file(exam: dict) -> str:
    """AnswerKey.load 형식 JSON을 기록하고 경로 반환."""
    k = exam["key"]
    payload = {
        "exam_id": exam["exam_id"], "title": exam["title"],
        "subject": "english" if exam["report_type"] == "english" else "",
        "default_point": k.get("default_point", 2),
        "grade_cuts": k.get("grade_cuts", []),
        "answers": k.get("answers", {}),
        "points": k.get("points", {}),
        "question_meta": k.get("question_meta", {}),
    }
    p = os.path.join(exam_dir(exam["exam_id"]), "answer_key.json")
    with open(p, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return p


def answer_key(exam: dict) -> AnswerKey | None:
    if not exam["key"].get("answers"):
        return None
    return AnswerKey.load(write_answer_key_file(exam))


# ---------------------------------------------------------------------------
# 영어 모의고사 표준 유형/배점/등급컷 자동 구성
# ---------------------------------------------------------------------------
_LISTEN = {
    1: "목적", 2: "의견·주장", 3: "관계·직업", 4: "그림 불일치", 5: "할 일", 6: "금액",
    7: "이유", 8: "언급하지 않은 것", 9: "내용 일치", 10: "도표(듣기)", 11: "짧은 대화 응답",
    12: "짧은 대화 응답", 13: "긴 대화 응답", 14: "긴 대화 응답", 15: "상황·부탁",
    16: "담화 주제", 17: "세부 내용",
}
_READ = {
    18: ("목적", "하"), 19: ("심경·분위기", "하"), 20: ("주장", "중"), 21: ("함의 추론", "상"),
    22: ("요지", "중"), 23: ("주제", "중"), 24: ("제목", "중"), 25: ("도표 일치", "중"),
    26: ("내용 일치", "하"), 27: ("실용문", "하"), 28: ("실용문", "하"), 29: ("어법", "상"),
    30: ("어휘", "상"), 31: ("빈칸 추론", "상"), 32: ("빈칸 추론", "상"), 33: ("빈칸 추론", "상"),
    34: ("빈칸 추론", "상"), 35: ("무관한 문장", "중"), 36: ("글의 순서", "상"), 37: ("글의 순서", "상"),
    38: ("문장 삽입", "상"), 39: ("문장 삽입", "상"), 40: ("문단 요약", "중"),
    41: ("장문 독해", "중"), 42: ("장문 독해", "중"), 43: ("장문 독해", "중"),
    44: ("장문 독해", "중"), 45: ("장문 독해", "중"),
}
_THREE_PT = {13, 21, 29, 31, 33, 34, 36, 38, 39, 42}


def apply_english_standard(exam: dict) -> None:
    """45문항 영어 모의고사 표준 유형·배점·등급컷을 채운다(정답은 비움)."""
    n = exam["sheet"]["num_questions"]
    meta, points = {}, {}
    for q in range(1, n + 1):
        if q in _LISTEN:
            meta[str(q)] = {"area": "듣기", "type": _LISTEN[q],
                            "difficulty": "상" if q in (13, 14, 17) else "중"}
        elif q in _READ:
            t, dfc = _READ[q]
            meta[str(q)] = {"area": "독해", "type": t, "difficulty": dfc}
        points[str(q)] = 3 if q in _THREE_PT else 2
    exam["key"]["question_meta"] = meta
    exam["key"]["points"] = points
    exam["key"]["default_point"] = 2
    exam["key"]["grade_cuts"] = [90, 80, 70, 60, 50, 40, 30, 20]
