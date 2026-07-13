"""파이프라인 자체 검증.

물리 스캐너 없이: 답안지 생성 → 가상 마킹(+왜곡) → 판독 → 원래 답과 대조.
학번·문항 응답 복원 정확도를 출력하고, 임계 이상이면 성공 처리한다.
"""
from __future__ import annotations

import os
import random

import cv2

from .layout import SheetConfig
from .generator import generate
from .reader import read_omr, ReadParams
from .simulate import simulate_marked


def run_selftest(out_dir: str, dpi: int = 200) -> bool:
    os.makedirs(out_dir, exist_ok=True)
    cfg = SheetConfig(exam_id="SELFTEST", title="자체검증 답안지",
                      num_questions=40, num_choices=5, id_digits=8)

    res = generate(cfg, out_dir, dpi=dpi, make_preview=False)
    template = res["template"]

    rng = random.Random(42)
    student_id = "20250042"
    answers = {q: rng.randint(1, cfg.num_choices) for q in range(1, cfg.num_questions + 1)}

    scan = simulate_marked(cfg, answers, student_id, dpi=dpi, distort=True, seed=7)
    scan_path = os.path.join(out_dir, "selftest_scan.png")
    cv2.imwrite(scan_path, scan)

    debug_path = os.path.join(out_dir, "selftest_debug.png")
    read = read_omr(scan_path, template, params=ReadParams(), debug_out=debug_path)

    got = read.answers()
    correct = sum(1 for q, v in answers.items() if got.get(q) == v)
    q_acc = correct / len(answers)

    sid_ok = read.resolved_student_id() == student_id

    print("=" * 56)
    print("OMR 파이프라인 자체 검증")
    print("=" * 56)
    print(f"시험코드 인식 : {read.exam_id}  (기대 SELFTEST)")
    print(f"학번 인식     : {read.resolved_student_id()}  (기대 {student_id})  -> {'OK' if sid_ok else 'FAIL'}")
    print(f"문항 정확도   : {correct}/{len(answers)} = {q_acc*100:.1f}%")
    print(f"검수 플래그   : {len(read.review_flags)}건")
    if read.review_flags[:5]:
        print("  예:", read.review_flags[:5])
    print(f"스캔본        : {scan_path}")
    print(f"판독 디버그   : {debug_path}")
    print("=" * 56)

    ok = sid_ok and q_acc >= 0.99 and read.exam_id == "SELFTEST"
    print("결과:", "성공 ✅" if ok else "실패 ❌")
    return ok
