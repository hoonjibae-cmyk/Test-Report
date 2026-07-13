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


def cmd_generate(a):
    cfg = SheetConfig(
        exam_id=a.exam, title=a.title, num_questions=a.questions,
        num_choices=a.choices, id_digits=a.id_digits,
        questions_per_column=a.per_column,
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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
