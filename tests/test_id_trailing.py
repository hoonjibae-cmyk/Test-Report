"""수험번호 뒤쪽 빈 자리 — 4자리 번호를 5자리 그리드에 쓰면 마지막 열이 빈다.

실행: python3 -m pytest -q tests/test_id_trailing.py

빈 열의 인쇄 글자·지운 자국·연필 스침 때문에 '2651?'가 되어 4자리 학생마다 사람이
확인해야 했다. 학생이 실제로 칠한 자리의 농도에 견줘 한참 옅은 뒤쪽 자국은 잘라내고,
마킹에 견줄 만큼 진한 자국만 '?'로 남긴다.
"""
import os
import random
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omr.generator import generate  # noqa: E402
from omr.layout import SheetConfig, build_layout  # noqa: E402
from omr.reader import GroupResult, ReadParams, assemble_student_id, read_omr  # noqa: E402
from omr.simulate import simulate_marked  # noqa: E402


def _ok(col, digit, fill=0.85, certain=True):
    fills = {d: 0.12 for d in range(10)}
    fills[digit] = fill
    return GroupResult(col, digit, "ok", fills, [digit], certain)


def _blank(col, darkest=None, fill=0.12, certain=True):
    fills = {d: 0.12 for d in range(10)}
    if darkest is not None:
        fills[darkest] = fill
    return GroupResult(col, None, "blank", fills, [], certain)


def _cols(*groups):
    return {g.key: g for g in groups}


def test_four_digit_id_with_faint_smudge_in_last_column_drops_the_column():
    cols = _cols(_ok(0, 2), _ok(1, 6), _ok(2, 5), _ok(3, 1), _blank(4, darkest=8, fill=0.27, certain=False))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "2651"
    assert [f["type"] for f in flags] == ["id_trailing"]
    assert flags[0]["col"] == 4 and flags[0]["candidate"] == "8"
    assert flags[0]["fill"] == 0.27 and flags[0]["mark_level"] == 0.85


def test_certain_blank_columns_are_still_dropped_silently():
    cols = _cols(_ok(0, 1), _ok(1, 2), _ok(2, 3), _ok(3, 4), _blank(4))
    assert assemble_student_id(cols, ReadParams()) == ("1234", [])


def test_faint_digit_above_the_mark_threshold_is_read_not_cut():
    # 마킹 임계를 넘은 자리는 흐려도 잘라내지 않는다 — 숫자로 읽고 weak 플래그를 남긴다.
    # 4614 학생인지 46140 학생인지는 웹이 명부로 정한다(lib/id-resolve).
    cols = _cols(_ok(0, 4), _ok(1, 6), _ok(2, 1), _ok(3, 4), _ok(4, 0, fill=0.36, certain=False))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "46140"
    assert flags == [{"type": "id", "col": 4, "status": "weak", "certain": False}]


def test_a_real_fifth_digit_is_kept_even_when_a_little_lighter():
    cols = _cols(_ok(0, 5), _ok(1, 2), _ok(2, 1), _ok(3, 3), _ok(4, 0, fill=0.6))
    assert assemble_student_id(cols, ReadParams()) == ("52130", [])


def test_light_marker_everywhere_keeps_the_question_mark():
    # 학생이 전체적으로 옅게 칠한 답안지(0.45) — 뒤쪽 0.28 자국은 마킹의 62%라 자르지 않는다
    cols = _cols(*(_ok(c, d, fill=0.45) for c, d in enumerate([2, 6, 5, 1])),
                 _blank(4, darkest=5, fill=0.28, certain=False))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "2651?"
    assert flags == [{"type": "id", "col": 4, "status": "review", "certain": False}]


def test_middle_uncertain_column_stays_a_question_mark():
    cols = _cols(_ok(0, 2), _ok(1, 6), _blank(2, darkest=5, fill=0.28, certain=False), _ok(3, 1), _blank(4))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "26?1"
    assert flags == [{"type": "id", "col": 2, "status": "review", "certain": False}]


def test_multiple_marks_in_last_column_are_never_dropped():
    fills = {d: 0.12 for d in range(10)}
    fills[3] = 0.33
    fills[8] = 0.31
    cols = _cols(_ok(0, 2), _ok(1, 6), _ok(2, 5), _ok(3, 1), GroupResult(4, 3, "multiple", fills, [3, 8], False))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "2651?"
    assert flags[0]["type"] == "id"


def test_nothing_marked_gives_no_level_to_compare_and_no_id():
    cols = _cols(*(_blank(c) for c in range(5)))
    assert assemble_student_id(cols, ReadParams()) == (None, [])
    # 읽힌 자리가 없으면 흐린 자국 규칙을 쓸 기준이 없다 — 지금처럼 '?'로 남긴다
    cols = _cols(*(_blank(c) for c in range(4)), _blank(4, darkest=8, fill=0.27, certain=False))
    assert assemble_student_id(cols, ReadParams())[0] == "?????"


def test_weak_but_read_digit_keeps_its_weak_flag():
    cols = _cols(_ok(0, 2), _ok(1, 6, fill=0.38, certain=False), _ok(2, 5), _ok(3, 1), _blank(4))
    sid, flags = assemble_student_id(cols, ReadParams())
    assert sid == "2651"
    assert flags == [{"type": "id", "col": 1, "status": "weak", "certain": False}]


def test_scanned_sheet_with_stray_dot_in_empty_fifth_column_reads_four_digits():
    """실제 판독 경로 — 4자리 번호에 빈 5열의 버블 하나에 점이 찍혀 있어도 4자리로 읽는다."""
    d = tempfile.mkdtemp()
    cfg = SheetConfig(exam_id="ID5", title="10월 월말평가", num_questions=25, num_choices=5,
                      id_digits=5, questions_per_column=20, style="exam")
    tpl = generate(cfg, d, dpi=200, make_preview=False)["template"]
    layout = build_layout(cfg)
    bubble = next(b for b in layout.bubbles if b.role == "id" and b.index == 4 and b.value == 8)
    mm2px = lambda v: int(round(v / 25.4 * 200))  # noqa: E731
    rng = random.Random(7)
    answers = {q: rng.randint(1, 5) for q in range(1, 26)}
    for seed in range(3):
        scan = simulate_marked(cfg, answers, "2651", dpi=200, distort=False, seed=seed)
        # 버블 반지름의 39% 되는 점 — 인쇄된 '8' 글자와 합쳐 채움률 0.27 안팎이 된다.
        # 마킹 임계(0.30)에는 못 미치지만 '확실한 빈칸' 선(0.255)은 넘어, 예전에는 '2651?'였다.
        cv2.circle(scan, (mm2px(bubble.x_mm), mm2px(bubble.y_mm)),
                   int(mm2px(layout.bubble_radius_mm) * 0.39), (40, 40, 40), -1)
        from omr.simulate import _apply_scan_distortion
        scan = _apply_scan_distortion(scan, seed)
        p = os.path.join(d, f"dot{seed}.png")
        cv2.imwrite(p, scan)
        r = read_omr(p, tpl, params=ReadParams())
        assert r.student_id_bubbles == "2651", (seed, r.student_id_bubbles, r.id_columns[4].fills)
        trailing = [f for f in r.review_flags if f["type"] == "id_trailing"]
        assert len(trailing) == 1 and trailing[0]["candidate"] == "8", r.review_flags
        assert not [f for f in r.review_flags if f["type"] == "id"], r.review_flags
        assert all(r.answers()[q] == v for q, v in answers.items())


def test_scanned_five_digit_id_is_untouched():
    d = tempfile.mkdtemp()
    cfg = SheetConfig(exam_id="ID5B", title="10월 월말평가", num_questions=25, num_choices=5,
                      id_digits=5, questions_per_column=20, style="exam")
    tpl = generate(cfg, d, dpi=200, make_preview=False)["template"]
    answers = {q: (q % 5) + 1 for q in range(1, 26)}
    scan = simulate_marked(cfg, answers, "52130", dpi=200, distort=True, seed=3)
    p = os.path.join(d, "five.png")
    cv2.imwrite(p, scan)
    r = read_omr(p, tpl, params=ReadParams())
    assert r.student_id_bubbles == "52130"
    assert r.review_flags == []
    assert isinstance(np.median([0.0]), float)
