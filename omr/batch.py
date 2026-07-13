"""일괄 처리: 스캔 폴더 → 판독 → 채점 → 웹 성적표.

한 폴더 안의 여러 스캔 이미지를 모두 판독하여 응시자별 답안을 모으고,
정답키로 채점한 뒤 웹링크 형식 성적표(HTML) 및 manifest를 생성한다.
manifest는 이후 알림톡 발송 단계의 입력이 된다.
"""
from __future__ import annotations

import glob
import json
import os

from .reader import read_omr, ReadParams
from .scorer import AnswerKey
from .report_web import build_reports, ExamMeta, DEFAULT_SALT


IMAGE_EXTS = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.bmp")


def read_folder(scan_dir: str, template_path: str, params: ReadParams | None = None,
                roster: dict | None = None) -> tuple[list, list]:
    """스캔 폴더를 판독해 records와 문제 목록을 반환한다.

    반환: (records, problems)
      records : [{"student_id","name","answers","review_flags","source"}]
      problems: 판독 실패/식별 불가 파일 목록
    """
    params = params or ReadParams()
    roster = roster or {}
    files = sorted(
        f for pat in IMAGE_EXTS for f in glob.glob(os.path.join(scan_dir, pat))
    )
    records, problems = [], []
    for path in files:
        try:
            r = read_omr(path, template_path, params=params)
        except Exception as e:
            problems.append({"file": os.path.basename(path), "error": str(e)})
            continue
        sid = r.resolved_student_id()
        if not sid or "?" in sid:
            problems.append({"file": os.path.basename(path),
                             "error": f"학번 식별 실패(sid={sid})"})
            continue
        records.append({
            "student_id": sid,
            "name": roster.get(sid, ""),
            "answers": r.answers(),
            "review_flags": r.review_flags,
            "source": os.path.basename(path),
        })
    return records, problems


def run_batch(scan_dir: str, template_path: str, key_path: str, out_dir: str,
              meta: ExamMeta, base_url: str = "", roster: dict | None = None,
              params: ReadParams | None = None, salt: str = DEFAULT_SALT) -> dict:
    key = AnswerKey.load(key_path)
    records, problems = read_folder(scan_dir, template_path, params=params, roster=roster)
    result = {"read": len(records), "problems": problems}
    if not records:
        result["reports"] = None
        return result
    built = build_reports(records, key, meta, out_dir, base_url=base_url, salt=salt)
    result["reports"] = built

    # 채점 결과 CSV(교사용)
    csv_path = os.path.join(built["dir"], "results.csv")
    with open(csv_path, "w", encoding="utf-8-sig") as fp:
        fp.write("석차,이름,학번,점수,만점,백분위,링크파일,원본스캔\n")
        by_sid = {r["student_id"]: r for r in records}
        for e in sorted(built["entries"], key=lambda x: x["rank"]):
            src = by_sid[e["student_id"]]["source"]
            fp.write(f'{e["rank"]},{e["name"]},{e["student_id"]},{e["score"]:g},'
                     f'{e["total"]:g},{e["percentile"]:g},{e["file"]},{src}\n')
    result["csv"] = csv_path
    return result
