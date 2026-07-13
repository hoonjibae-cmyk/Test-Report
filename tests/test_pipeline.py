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
    assert rr.answers()[1] is None      # 이중마킹은 채점되지 않음
    assert rr.answers()[2] is None


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
