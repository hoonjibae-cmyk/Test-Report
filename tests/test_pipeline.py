"""OMR 파이프라인 회귀 테스트.

물리 스캐너 없이 생성→가상마킹→판독→채점을 검증한다.
실행: python -m pytest tests/ -v   (pytest 미설치 시 아래 __main__ 로 실행)
"""
import os
import random
import sys
import tempfile

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omr.layout import SheetConfig, build_layout
from omr.generator import generate, render_image
from omr.reader import read_omr, ReadParams
from omr.simulate import simulate_marked
from omr.scorer import AnswerKey, score_one, score_batch


def _tmp():
    return tempfile.mkdtemp(prefix="omr_test_")


def test_generate_outputs():
    d = _tmp()
    cfg = SheetConfig(exam_id="T1", num_questions=20, num_choices=5, id_digits=6)
    res = generate(cfg, d, dpi=150)
    assert os.path.exists(res["template"])
    assert res["pdfs"] and os.path.exists(res["pdfs"][0])
    assert res["previews"] and os.path.exists(res["previews"][0])


def test_all_bubbles_inside_reference_rect():
    # 모든 버블의 정규화 좌표가 [0,1] 안(=마커 사각형 내부)이어야 함
    cfg = SheetConfig(num_questions=40, id_digits=8)
    layout = build_layout(cfg)
    for b in layout.bubbles:
        assert -0.001 <= b.u <= 1.001, (b.role, b.index, b.value, b.u)
        assert -0.001 <= b.v <= 1.001, (b.role, b.index, b.value, b.v)


def test_read_roundtrip_accuracy():
    d = _tmp()
    cfg = SheetConfig(exam_id="T2", num_questions=40, num_choices=5, id_digits=8)
    res = generate(cfg, d, dpi=200, make_preview=False)
    tpl = res["template"]

    total = correct = sids = 0
    for t in range(8):
        rng = random.Random(t)
        sid = "".join(str(rng.randint(0, 9)) for _ in range(8))
        ans = {q: rng.randint(1, 5) for q in range(1, 41)}
        scan = simulate_marked(cfg, ans, sid, dpi=200, distort=True, seed=50 + t)
        p = os.path.join(d, f"s{t}.png")
        cv2.imwrite(p, scan)
        r = read_omr(p, tpl, params=ReadParams())
        got = r.answers()
        for q, v in ans.items():
            total += 1
            correct += int(got.get(q) == v)
        sids += int(r.resolved_student_id() == sid)

    assert correct / total >= 0.99, f"accuracy {correct}/{total}"
    assert sids == 8, f"student id {sids}/8"


def test_exam_style_roundtrip_and_left_aligned_id():
    d = _tmp()
    cfg = SheetConfig(exam_id="EX", title="모의고사 답안지", num_questions=45,
                      num_choices=5, id_digits=5, questions_per_column=20,
                      style="exam", period="3", subject_label="영어 영역",
                      academy="테스트학원")
    res = generate(cfg, d, dpi=200, make_preview=False)
    tpl = res["template"]

    # 모든 버블이 마커 사각형 안에 있어야 함(가로형)
    layout = build_layout(cfg)
    for b in layout.bubbles:
        assert -0.001 <= b.u <= 1.001 and -0.001 <= b.v <= 1.001

    # 4자리·5자리 좌측정렬 학번 모두 복원되고 45/45
    for sid in ["1234", "52130"]:
        rng = random.Random(len(sid))
        ans = {q: rng.randint(1, 5) for q in range(1, 46)}
        scan = simulate_marked(cfg, ans, sid, dpi=210, distort=True, seed=len(sid))
        p = os.path.join(d, f"e{sid}.png")
        cv2.imwrite(p, scan)
        r = read_omr(p, tpl, params=ReadParams())
        correct = sum(1 for q, v in ans.items() if r.answers().get(q) == v)
        assert correct == 45, f"{sid}: {correct}/45"
        assert r.student_id_bubbles == sid, r.student_id_bubbles
        assert len(r.review_flags) == 0


def test_exam_essay_and_correction_tape():
    """서술형(주관식) 손기입 칸과 수정테이프가 객관식 판독을 방해하지 않는다."""
    import numpy as np
    from omr.generator import render_exam_image

    d = _tmp()
    cfg = SheetConfig(exam_id="ESY", title="4월 월말평가", num_questions=20,
                      num_choices=5, id_digits=5, questions_per_column=20,
                      style="exam", essay_count=3, academy="테스트학원")
    res = generate(cfg, d, dpi=210, make_preview=False)
    tpl = res["template"]
    layout = build_layout(cfg)

    # 서술형 칸은 판독 대상 버블이 아니어야 한다(객관식 20문항 버블만 존재).
    assert len(layout.essay_boxes_mm) == 3
    qidx = {b.index for b in layout.bubbles if b.role == "question"}
    assert qidx == set(range(1, 21))

    dpi = 210

    def mm2px(v):
        return int(round(v / 25.4 * dpi))

    img = cv2.cvtColor(np.array(render_exam_image(layout, dpi=dpi)), cv2.COLOR_RGB2BGR)
    bub = {(b.role, b.index, b.value): b for b in layout.bubbles}
    r = int(mm2px(layout.bubble_radius_mm) * 0.8)

    def fill(b):
        cv2.circle(img, (mm2px(b.x_mm), mm2px(b.y_mm)), r, (35, 35, 35), -1)

    ans = {i: ((i * 2) % 5) + 1 for i in range(1, 21)}  # ans[3] == 2
    # 3번: 보기4로 잘못 표기 후 수정테이프(밝은 회색)로 덮고 정답 표기
    wrong = bub[("question", 3, 3)]
    fill(wrong)
    cx, cy = mm2px(wrong.x_mm), mm2px(wrong.y_mm)
    cv2.rectangle(img, (cx - int(r * 1.7), cy - int(r * 1.4)),
                  (cx + int(r * 1.7), cy + int(r * 1.4)), (236, 236, 236), -1)
    for q, c in ans.items():
        fill(bub[("question", q, c - 1)])
    sid = "20421"
    for col, ch in enumerate(sid):
        fill(bub[("id", col, int(ch))])
    # 서술형 칸 전체에 손글씨 낙서
    for eb in layout.essay_boxes_mm:
        x0, y0, x1, y1 = (mm2px(eb["x0"]), mm2px(eb["y0"]), mm2px(eb["x1"]), mm2px(eb["y1"]))
        for i in range(6):
            yy = y0 + int((y1 - y0) * (0.25 + 0.1 * i))
            pts = np.array([[x0 + 20 + j * 25, yy + int(12 * np.sin(j * 0.9 + i))]
                            for j in range(20)], np.int32)
            cv2.polylines(img, [pts], False, (30, 30, 30), 3)

    rng = np.random.default_rng(3)
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = (src + rng.uniform(-0.01, 0.01, src.shape) * [w, h]).astype(np.float32)
    img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h),
                              borderValue=(255, 255, 255))
    img = np.clip(img.astype(np.float32) + rng.normal(0, 5, img.shape), 0, 255).astype(np.uint8)

    p = os.path.join(d, "essay_scan.png")
    cv2.imwrite(p, img)
    rr = read_omr(p, tpl, params=ReadParams())
    got = rr.answers()
    assert all(got.get(q) == ans[q] for q in ans), got   # 수정테이프 정정 포함 전 문항 정확
    assert got.get(3) == ans[3]                            # 테이프로 가린 오답은 무시
    assert rr.student_id_bubbles == sid
    assert len(rr.review_flags) == 0                       # 서술형 낙서로 인한 오검출 없음


def test_ambiguous_flagged_not_scored():
    d = _tmp()
    cfg = SheetConfig(exam_id="T3", num_questions=10, num_choices=5, id_digits=4)
    res = generate(cfg, d, dpi=200, make_preview=False)
    layout = build_layout(cfg)
    import numpy as np

    img = cv2.cvtColor(np.array(render_image(layout, dpi=200)), cv2.COLOR_RGB2BGR)

    def mm2px(v):
        return int(round(v / 25.4 * 200))

    r = int(mm2px(layout.bubble_radius_mm) * 0.8)
    bub = {(b.role, b.index, b.value): b for b in layout.bubbles}

    def fill(b):
        cv2.circle(img, (mm2px(b.x_mm), mm2px(b.y_mm)), r, (35, 35, 35), -1)

    fill(bub[("question", 1, 0)])
    fill(bub[("question", 1, 2)])   # Q1 이중마킹
    fill(bub[("question", 3, 3)])   # Q3 단일마킹
    for col, ch in enumerate("0007"):
        fill(bub[("id", col, int(ch))])
    p = os.path.join(d, "edge.png")
    cv2.imwrite(p, img)

    rr = read_omr(p, res["template"], params=ReadParams())
    assert rr.questions[1].status == "multiple"
    assert rr.questions[2].status == "blank"
    assert rr.questions[3].status == "ok" and rr.questions[3].chosen == 3
    assert rr.answers()[1] is None      # 단일 선택 기준으로는 채점되지 않음
    assert rr.answers()[2] is None
    # '모두 고르기' 문항 대응: 칠해진 보기를 전부 돌려준다(1-base).
    assert rr.selections()[1] == [1, 3]
    assert rr.selections()[2] == []
    assert rr.selections()[3] == [4]
    flag = next(f for f in rr.review_flags if f.get("type") == "question" and f["no"] == 1)
    assert flag["selected"] == [1, 3]


def test_multi_select_many_choices():
    """세 개를 칠한 '모두 고르기' 문항도 전부 읽어야 한다."""
    d = _tmp()
    cfg = SheetConfig(exam_id="T9", num_questions=8, num_choices=5, id_digits=4)
    res = generate(cfg, d, dpi=200, make_preview=False)
    layout = build_layout(cfg)
    import numpy as np

    img = cv2.cvtColor(np.array(render_image(layout, dpi=200)), cv2.COLOR_RGB2BGR)
    mm2px = lambda v: int(round(v / 25.4 * 200))  # noqa: E731
    r = int(mm2px(layout.bubble_radius_mm) * 0.8)
    bub = {(b.role, b.index, b.value): b for b in layout.bubbles}
    for value in (0, 2, 4):
        b = bub[("question", 2, value)]
        cv2.circle(img, (mm2px(b.x_mm), mm2px(b.y_mm)), r, (35, 35, 35), -1)
    p = os.path.join(d, "multi.png")
    cv2.imwrite(p, img)

    rr = read_omr(p, res["template"], params=ReadParams())
    assert rr.selections()[2] == [1, 3, 5]
    assert rr.questions[2].status == "multiple"


def test_exam_layout_never_produces_broken_sheet():
    """설정이 종이에 안 맞아도 깨진 답안지를 만들지 않는다.

    예전에는 열이 넘칠 때 ① 서술형 칸 좌표가 뒤집혀 렌더링이 예외로 죽거나
    ② 마지막 열이 종이 밖으로 나갔다. 이제는 배치를 조정하거나, 그래도 안 되면
    사람이 읽을 수 있는 오류를 낸다 — 어느 쪽이든 '조용히 깨진 답안지'는 없다.
    """
    import itertools

    from omr.layout import build_exam_layout

    checked = rejected = 0
    for n, ch, dig, per, essays in itertools.product(
        (10, 40, 45, 50, 60, 100), (4, 5), (4, 5, 8), (15, 20, 25), (0, 3, 5)
    ):
        cfg = SheetConfig(exam_id="T", num_questions=n, num_choices=ch, id_digits=dig,
                          questions_per_column=per, style="exam", essay_count=essays)
        try:
            layout = build_exam_layout(cfg)
        except ValueError:
            rejected += 1      # 한 장에 못 담는 조합 — 명확한 오류로 거절
            continue
        combo = (n, ch, dig, per, essays)
        r = layout.bubble_radius_mm
        qb = [b for b in layout.bubbles if b.role == "question"]
        right = max(b.x_mm for b in qb) + r

        assert right <= layout.ref_right_mm + 0.01, f"{combo}: 문항이 종이 밖으로 나감"
        assert max(b.y_mm for b in qb) + r <= layout.ref_bottom_mm, f"{combo}: 아래로 벗어남"
        # 수험번호 그리드(좌측 패널)를 침범하지 않아야 한다
        id_right = layout.id_origin_mm[0] + (dig - 1) * layout.id_col_pitch_mm
        assert min(b.x_mm for b in qb) - r > id_right, f"{combo}: 수험번호 패널 침범"
        # 버블이 서로 닿으면 판독이 무너진다
        assert layout.q_choice_pitch_mm >= 2 * r + 0.7, f"{combo}: 보기 간격 부족"
        assert layout.q_row_pitch_mm >= 2 * r + 0.7, f"{combo}: 행 간격 부족"
        if layout.essay_boxes_mm:
            box = layout.essay_boxes_mm[0]
            assert box["x0"] > right, f"{combo}: 서술형 칸이 객관식 버블과 겹침"
            assert box["x1"] > box["x0"], f"{combo}: 서술형 칸 좌표가 뒤집힘"
        checked += 1

    assert checked > 200, f"검사한 조합이 너무 적습니다({checked})"
    assert rejected < checked, "대부분의 조합이 거절되면 배치 로직이 과하게 엄격한 것"


def test_renderer_uses_effective_per_column():
    """설정이 조정되면 렌더러도 조정된 값으로 문항을 열에 나눠야 한다.

    Layout.questions_per_column 대신 config 값을 쓰면 문항 번호가 버블과
    어긋나 답안지가 통째로 못 쓰게 된다.
    """
    from omr.layout import build_exam_layout

    cfg = SheetConfig(exam_id="T", num_questions=45, num_choices=5, id_digits=8,
                      questions_per_column=15, style="exam", essay_count=3)
    layout = build_exam_layout(cfg)
    assert layout.questions_per_column != cfg.questions_per_column, "조정이 일어나는 설정이어야 함"

    # 실제 열 개수 = 서로 다른 첫 보기 x좌표의 개수
    first_xs = {round(b.x_mm, 1) for b in layout.bubbles if b.role == "question" and b.value == 0}
    expected = -(-cfg.num_questions // layout.questions_per_column)
    assert len(first_xs) == expected, f"열 개수 불일치: {len(first_xs)} != {expected}"

    # 같은 열의 문항들은 x가 같고 y가 순증해야 한다
    per_col = layout.questions_per_column
    for q in range(1, cfg.num_questions):
        a = next(b for b in layout.bubbles if b.role == "question" and b.index == q and b.value == 0)
        b_ = next(b for b in layout.bubbles if b.role == "question" and b.index == q + 1 and b.value == 0)
        if (q - 1) // per_col == q // per_col:
            assert abs(a.x_mm - b_.x_mm) < 0.01, f"{q}→{q+1}: 같은 열인데 x가 다름"
            assert b_.y_mm > a.y_mm, f"{q}→{q+1}: 위→아래 순서가 아님"


def test_essay_panel_does_not_overlap_bubbles():
    """서술형 손기입 칸이 객관식 버블 위에 겹치면 판독이 깨진다 — 항상 오른쪽에 비켜 있어야."""
    for n, per_col, essays in ((45, 20, 5), (45, 15, 5), (40, 20, 5), (60, 20, 3)):
        cfg = SheetConfig(exam_id="T", num_questions=n, num_choices=5, id_digits=5,
                          questions_per_column=per_col, style="exam", essay_count=essays)
        layout = build_layout(cfg)
        obj_right = max(b.x_mm for b in layout.bubbles if b.role == "question")
        obj_right += layout.bubble_radius_mm
        assert layout.essay_boxes_mm, f"{n}/{per_col}: 서술형 칸이 없음"
        assert layout.essay_boxes_mm[0]["x0"] > obj_right, (
            f"{n}문항/{per_col}단: 서술형 칸이 객관식 버블과 겹칩니다."
        )


def test_web_reports_and_manifest():
    import json as _json
    from omr.report_web import build_reports, ExamMeta, make_token
    from omr.scorer import AnswerKey as _AK

    key = _AK("WEB", "t", {1: 1, 2: 2, 3: 3, 4: 4}, {q: 25.0 for q in (1, 2, 3, 4)})
    records = [
        {"student_id": "20250001", "name": "가나", "answers": {1: 1, 2: 2, 3: 3, 4: 4}},
        {"student_id": "20250002", "name": "다라", "answers": {1: 1, 2: 5, 3: 3, 4: None},
         "review_flags": [{"type": "question", "no": 4, "status": "blank"}]},
    ]
    d = _tmp()
    meta = ExamMeta(exam_id="WEB", title="테스트", date="2026-01-01", school="테스트중")
    out = build_reports(records, key, meta, d,
                        base_url="https://x.example/web", salt="fixed-salt")

    # 산출물 존재
    assert os.path.exists(out["manifest"]) and os.path.exists(out["index"])
    for e in out["entries"]:
        assert os.path.exists(os.path.join(out["dir"], e["file"]))
        assert e["url"].startswith("https://x.example/web/")

    # 토큰 결정론: 같은 salt/exam/sid → 같은 토큰(링크 안정성)
    assert make_token("fixed-salt", "WEB", "20250001") == out["entries"][0]["token"] \
        if out["entries"][0]["student_id"] == "20250001" else True
    t1 = make_token("fixed-salt", "WEB", "20250001")
    assert t1 == make_token("fixed-salt", "WEB", "20250001")
    assert t1 != make_token("other-salt", "WEB", "20250001")

    # manifest 내용
    m = _json.load(open(out["manifest"], encoding="utf-8"))
    assert m["count"] == 2 and m["exam"]["exam_id"] == "WEB"

    # 만점자가 1등
    top = min(out["entries"], key=lambda x: x["rank"])
    assert top["student_id"] == "20250001" and top["score"] == 100.0

    # HTML에 이름과 점수가 포함
    html_path = os.path.join(out["dir"], out["entries"][0]["file"])
    doc = open(html_path, encoding="utf-8").read()
    assert "가나" in doc or "다라" in doc


def test_batch_reads_folder():
    import numpy as np
    from omr.batch import run_batch
    from omr.report_web import ExamMeta as _EM
    from omr.scorer import AnswerKey as _AK

    d = _tmp()
    cfg = SheetConfig(exam_id="B1", num_questions=20, num_choices=5, id_digits=8)
    res = generate(cfg, d, dpi=200, make_preview=False)
    tpl = res["template"]

    # 정답키 + 스캔 2장 생성
    ans_key = {q: ((q % 5) + 1) for q in range(1, 21)}
    import json as _json
    key_path = os.path.join(d, "key.json")
    _json.dump({"exam_id": "B1", "title": "b", "default_point": 5.0,
                "answers": {str(k): v for k, v in ans_key.items()}}, open(key_path, "w"))

    scans_dir = os.path.join(d, "scans")
    os.makedirs(scans_dir)
    for sid, ans in [("20250001", ans_key),
                     ("20250002", {q: 1 for q in range(1, 21)})]:
        scan = simulate_marked(cfg, ans, sid, dpi=200, distort=True, seed=hash(sid) % 100)
        cv2.imwrite(os.path.join(scans_dir, f"{sid}.png"), scan)

    meta = _EM(exam_id="B1", title="b")
    out = run_batch(scans_dir, tpl, key_path, d, meta, base_url="", salt="s")
    assert out["read"] == 2
    assert out["reports"] and len(out["reports"]["entries"]) == 2
    assert os.path.exists(out["csv"])


def test_english_analysis_and_grade():
    from omr.scorer import AnswerKey as _AK, english_analysis, compute_grade

    assert compute_grade(90, [90, 80, 70]) == 1
    assert compute_grade(85, [90, 80, 70]) == 2
    assert compute_grade(10, [90, 80, 70]) == 4   # 마지막 컷 미만
    assert compute_grade(50, []) is None

    key = _AK(
        exam_id="E", title="영어", subject="english",
        answers={1: 1, 2: 2, 3: 3, 4: 4},
        points={1: 25.0, 2: 25.0, 3: 25.0, 4: 25.0},
        grade_cuts=[90, 80, 70, 60, 50],
        qmeta={1: {"area": "듣기", "type": "목적", "difficulty": "하"},
               2: {"area": "듣기", "type": "그림", "difficulty": "중"},
               3: {"area": "독해", "type": "빈칸", "difficulty": "상"},
               4: {"area": "독해", "type": "순서", "difficulty": "상"}},
    )
    # 듣기 2문항 모두 정답(50점), 독해 2문항 오답(0점) → 원점수 50 → 5등급
    ans = {1: 1, 2: 2, 3: 5, 4: 5}
    a = english_analysis(ans, key)
    assert a["grade"] == 5
    areas = {s["name"]: s for s in a["area_stats"]}
    assert areas["듣기"]["rate"] == 100.0 and areas["독해"]["rate"] == 0.0
    assert areas["듣기"]["correct"] == 2 and areas["독해"]["correct"] == 0
    # 난이도 '상'은 전부 오답
    diffs = {s["name"]: s["rate"] for s in a["difficulty_stats"]}
    assert diffs["상"] == 0.0


def test_reading_category_grouping():
    from omr.scorer import AnswerKey as _AK, reading_category_stats

    # 세부 유형이 대분류로 묶이고, 듣기는 제외되는지 확인
    key = _AK(
        exam_id="E", title="영어", subject="english",
        answers={1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
        points={q: 2.0 for q in range(1, 6)},
        qmeta={
            1: {"area": "듣기", "type": "목적"},          # 듣기 → 제외
            2: {"area": "독해", "type": "요지"},          # 대의 파악
            3: {"area": "독해", "type": "주제"},          # 대의 파악
            4: {"area": "독해", "type": "빈칸 추론"},      # 빈칸 추론
            5: {"area": "독해", "type": "글의 순서"},      # 간접 쓰기
        },
    )
    stats = reading_category_stats({1: 1, 2: 1, 3: 2, 4: 1, 5: 2}, key)
    names = {s["name"]: s for s in stats}
    assert "듣기" not in " ".join(names)          # 듣기 대분류 없음(영역 카드에서 표시)
    assert "대의 파악" in names and names["대의 파악"]["count"] == 2
    assert "빈칸 추론" in names and "간접 쓰기" in names
    # 대분류 표시는 교육과정 순서(대의 파악이 빈칸/간접보다 앞)
    order = [s["name"] for s in stats]
    assert order.index("대의 파악") < order.index("빈칸 추론") < order.index("간접 쓰기")


def test_english_report_build():
    import json as _json
    from omr.report_web import build_reports, ExamMeta
    from omr.scorer import AnswerKey as _AK

    key = _AK(
        exam_id="ENG", title="영어모의", subject="english",
        answers={q: 1 for q in range(1, 11)},
        points={q: 10.0 for q in range(1, 11)},
        grade_cuts=[90, 80, 70, 60, 50],
        qmeta={q: {"area": "듣기" if q <= 5 else "독해",
                   "type": f"유형{q}", "difficulty": "중"} for q in range(1, 11)},
    )
    records = [
        {"student_id": "20250001", "name": "홍길동",
         "answers": {q: (1 if q <= 8 else 2) for q in range(1, 11)}},   # 80점 → 2등급
        {"student_id": "20250002", "name": "김영희",
         "answers": {q: (1 if q <= 4 else 2) for q in range(1, 11)}},   # 40점
    ]
    d = _tmp()
    meta = ExamMeta(exam_id="ENG", title="영어모의", school="테스트중", report_type="english")
    out = build_reports(records, key, meta, d, salt="s")
    top = min(out["entries"], key=lambda x: x["rank"])
    assert top["grade"] == 2
    doc = open(os.path.join(out["dir"], top["file"]), encoding="utf-8").read()
    assert "등급" in doc and "영역별 성취" in doc and "유형별 성취율" in doc
    assert "Pretendard" in doc                    # 폰트 링크 포함
    assert "bar-mark" in doc and "집단 평균" in doc  # 집단 평균 마커 포함


def test_scorer_stats():
    key = AnswerKey("E", "t", {1: 1, 2: 2, 3: 3, 4: 4}, {1: 1, 2: 1, 3: 1, 4: 1})
    recs = [
        {"student_id": "A", "answers": {1: 1, 2: 2, 3: 3, 4: 4}},  # 4
        {"student_id": "B", "answers": {1: 1, 2: 2, 3: 1, 4: 1}},  # 2
        {"student_id": "C", "answers": {1: 5, 2: 5, 3: 5, 4: 5}},  # 0
    ]
    out = score_batch(recs, key)
    by = {o["student_id"]: o for o in out}
    assert by["A"]["raw_score"] == 4 and by["A"]["rank"] == 1
    assert by["C"]["rank"] == 3
    assert by["A"]["class_size"] == 3


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
