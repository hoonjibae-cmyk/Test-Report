"""OMR 시스템 CLI.

사용 예:
  python -m omr.cli generate --exam MID2026 --title "1학기 중간고사" \
        --questions 40 --choices 5 --id-digits 8 --out output
  python -m omr.cli read  --image scan.png --template output/MID2026_template.json
  python -m omr.cli score --image scan.png --template output/MID2026_template.json \
        --key examples/answer_key.json
  python -m omr.cli selftest --out output
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .layout import SheetConfig
from .generator import generate
from .reader import read_omr, ReadParams
from .scorer import AnswerKey, score_one
from .report_web import DEFAULT_SALT


def cmd_generate(a):
    cfg = SheetConfig(
        exam_id=a.exam, title=a.title, num_questions=a.questions,
        num_choices=a.choices, id_digits=a.id_digits,
        questions_per_column=a.per_column,
        style=a.style, period=a.period, subject_label=a.subject, academy=a.academy,
        academy_logo=a.academy_logo,
    )
    students = None
    if a.students:
        with open(a.students, encoding="utf-8") as fp:
            students = json.load(fp)
    res = generate(cfg, a.out, dpi=a.dpi, students=students, make_preview=not a.no_preview)
    print("생성 완료:")
    print("  템플릿:", res["template"])
    for p in res["pdfs"]:
        print("  PDF   :", p)
    for p in res["previews"]:
        print("  미리보기:", p)


def cmd_read(a):
    params = ReadParams(mark_abs_min=a.threshold)
    res = read_omr(a.image, a.template, params=params, debug_out=a.debug)
    out = {
        "exam_id": res.exam_id,
        "student_id": res.resolved_student_id(),
        "student_id_qr": res.student_id_qr,
        "student_id_bubbles": res.student_id_bubbles,
        "answers": res.answers(),
        "review_flags": res.review_flags,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.debug:
        print(f"(디버그 이미지: {a.debug})", file=sys.stderr)


def cmd_score(a):
    key = AnswerKey.load(a.key)
    params = ReadParams(mark_abs_min=a.threshold)
    res = read_omr(a.image, a.template, params=params)
    s = score_one(res.answers(), key)
    out = {
        "exam_id": res.exam_id,
        "student_id": res.resolved_student_id(),
        "score": s,
        "review_flags": res.review_flags,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_selftest(a):
    """생성→가상마킹→판독까지 자체 검증."""
    from .selftest import run_selftest
    ok = run_selftest(a.out)
    sys.exit(0 if ok else 1)


def _load_roster(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as fp:
        data = json.load(fp)
    # [{"id","name"}] 또는 {"학번":"이름"} 모두 허용
    if isinstance(data, list):
        return {str(x["id"]): x.get("name", "") for x in data}
    return {str(k): v for k, v in data.items()}


def _resolve_report_type(cli_value, key_path):
    """--report-type 'auto'면 정답키 subject로 추론."""
    if cli_value != "auto":
        return cli_value
    try:
        with open(key_path, encoding="utf-8") as fp:
            return "english" if json.load(fp).get("subject") == "english" else "basic"
    except Exception:
        return "basic"


def cmd_batch(a):
    """스캔 폴더 → 판독 → 채점 → 웹 성적표 일괄 생성."""
    from .batch import run_batch
    from .report_web import ExamMeta

    rtype = _resolve_report_type(a.report_type, a.key)
    meta = ExamMeta(exam_id=a.exam, title=a.title, date=a.date, school=a.school, report_type=rtype)
    params = ReadParams(mark_abs_min=a.threshold)
    res = run_batch(
        scan_dir=a.scans, template_path=a.template, key_path=a.key, out_dir=a.out,
        meta=meta, base_url=a.base_url, roster=_load_roster(a.roster),
        params=params, salt=a.salt,
    )
    print(f"판독 성공: {res['read']}명")
    if res["problems"]:
        print(f"확인 필요 파일: {len(res['problems'])}건")
        for p in res["problems"]:
            print("  -", p["file"], ":", p["error"])
    if res.get("reports"):
        b = res["reports"]
        print("웹 성적표 폴더 :", b["dir"])
        print("교사용 목록    :", b["index"])
        print("알림톡 manifest:", b["manifest"])
        print("결과 CSV       :", res["csv"])
        print(f"성적표 링크 {len(b['entries'])}건 생성 완료")


def cmd_report(a):
    """이미 판독/채점된 records JSON으로 웹 성적표만 생성."""
    from .report_web import build_reports, ExamMeta

    with open(a.records, encoding="utf-8") as fp:
        records = json.load(fp)
    for r in records:  # answers 키를 int로 정규화
        r["answers"] = {int(k): v for k, v in r["answers"].items()}
    key = AnswerKey.load(a.key)
    rtype = _resolve_report_type(a.report_type, a.key)
    meta = ExamMeta(exam_id=a.exam, title=a.title, date=a.date, school=a.school, report_type=rtype)
    built = build_reports(records, key, meta, a.out, base_url=a.base_url, salt=a.salt)
    print("웹 성적표 폴더 :", built["dir"])
    print("교사용 목록    :", built["index"])
    print("알림톡 manifest:", built["manifest"])


def cmd_serve(a):
    """생성된 성적표를 로컬에서 미리보기(정적 서버)."""
    import http.server
    import socketserver

    os.chdir(a.dir)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", a.port), handler) as httpd:
        print(f"미리보기: http://localhost:{a.port}/  (Ctrl+C 종료)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n종료")


def build_parser():
    p = argparse.ArgumentParser(prog="omr", description="OMR 생성·판독·채점 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="OMR PDF/템플릿 생성")
    g.add_argument("--exam", default="EXAM")
    g.add_argument("--title", default="OMR 답안지")
    g.add_argument("--questions", type=int, default=40)
    g.add_argument("--choices", type=int, default=5)
    g.add_argument("--id-digits", type=int, default=8)
    g.add_argument("--per-column", type=int, default=20)
    g.add_argument("--style", default="basic", choices=["basic", "exam"],
                   help="basic=세로 단순형 / exam=수능형 가로")
    g.add_argument("--period", default="", help="교시(예: 3) — exam 스타일")
    g.add_argument("--subject", default="", help="영역 표기(예: 영어 영역) — exam 스타일")
    g.add_argument("--academy", default="", help="학원명(구석 로고) — exam 스타일")
    g.add_argument("--academy-logo", default="", help="학원 로고 이미지 경로 — exam 스타일")
    g.add_argument("--students", help="응시자 목록 JSON([{id,name}])")
    g.add_argument("--dpi", type=int, default=200)
    g.add_argument("--no-preview", action="store_true")
    g.add_argument("--out", default="output")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("read", help="스캔 이미지 판독")
    r.add_argument("--image", required=True)
    r.add_argument("--template", required=True)
    r.add_argument("--threshold", type=float, default=0.30)
    r.add_argument("--debug", help="판독 디버그 이미지 경로")
    r.set_defaults(func=cmd_read)

    s = sub.add_parser("score", help="판독 후 채점")
    s.add_argument("--image", required=True)
    s.add_argument("--template", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--threshold", type=float, default=0.30)
    s.set_defaults(func=cmd_score)

    t = sub.add_parser("selftest", help="파이프라인 자체 검증")
    t.add_argument("--out", default="output")
    t.set_defaults(func=cmd_selftest)

    b = sub.add_parser("batch", help="스캔 폴더 일괄 판독·채점·웹성적표 생성")
    b.add_argument("--scans", required=True, help="스캔 이미지 폴더")
    b.add_argument("--template", required=True)
    b.add_argument("--key", required=True)
    b.add_argument("--exam", default="EXAM")
    b.add_argument("--title", default="시험")
    b.add_argument("--date", default="")
    b.add_argument("--school", default="")
    b.add_argument("--roster", help="학번→이름 매핑 JSON(선택)")
    b.add_argument("--base-url", default="", help="링크 접두사(예: https://reports.school.kr/mid2026)")
    b.add_argument("--salt", default=DEFAULT_SALT, help="링크 토큰 salt(운영 시 반드시 고정·비공개)")
    b.add_argument("--report-type", default="auto", choices=["auto", "basic", "english"],
                   help="성적표 유형(auto=정답키 subject로 추론)")
    b.add_argument("--threshold", type=float, default=0.30)
    b.add_argument("--out", default="output")
    b.set_defaults(func=cmd_batch)

    rp = sub.add_parser("report", help="records JSON으로 웹 성적표 생성")
    rp.add_argument("--records", required=True, help='[{student_id,name,answers,review_flags?}]')
    rp.add_argument("--key", required=True)
    rp.add_argument("--exam", default="EXAM")
    rp.add_argument("--title", default="시험")
    rp.add_argument("--date", default="")
    rp.add_argument("--school", default="")
    rp.add_argument("--base-url", default="")
    rp.add_argument("--salt", default=DEFAULT_SALT)
    rp.add_argument("--report-type", default="auto", choices=["auto", "basic", "english"])
    rp.add_argument("--out", default="output")
    rp.set_defaults(func=cmd_report)

    sv = sub.add_parser("serve", help="생성된 성적표 로컬 미리보기")
    sv.add_argument("--dir", required=True, help="성적표 폴더(reports/<exam>)")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
