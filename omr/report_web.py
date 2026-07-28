"""웹링크 형식 성적표 생성.

응시자별로 '자체완결형(single-file) HTML 성적표'를 만들고, 추측 불가능한
토큰이 붙은 파일명으로 저장한다. 이 파일들은 정적 호스팅(S3/Netlify/학교 서버 등)
어디에 올려도 링크만으로 열람 가능하며, 링크는 이후 알림톡의 링크 변수로 재사용된다.

토큰은 (salt, exam_id, student_id)로부터 결정론적으로 파생되므로, 재생성해도
동일 응시자의 링크가 유지된다(알림톡 발송 후에도 링크가 안 바뀜).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from dataclasses import dataclass

from .scorer import AnswerKey, score_one, score_batch


DEFAULT_SALT = os.environ.get("OMR_REPORT_SALT", "omr-demo-salt-change-me")


def make_token(salt: str, exam_id: str, student_id: str) -> str:
    msg = f"{exam_id}|{student_id}".encode()
    return hmac.new(salt.encode(), msg, hashlib.sha256).hexdigest()[:20]


@dataclass
class ExamMeta:
    exam_id: str
    title: str
    date: str = ""
    school: str = ""


# ----------------------------------------------------------------------------
# HTML 렌더링 (자체완결형, 모바일 우선)
# 목동유쌤영어학원 리포트 톤앤매너(네이비+골드)를 따른 디자인 토큰.
# 여러 성적표 유형이 이 토큰/컴포넌트 CSS를 공유하도록 별도 함수로 분리한다.
REPORT_CSS = """
  :root{
    --navy:#183c73; --navy-deep:#102b55; --blue:#2f67b1; --blue-soft:#eaf2fb;
    --gold:#d2a93b; --gold-soft:#fff8df; --ink:#172033; --muted:#667085;
    --line:#d9e0ea; --surface:#ffffff; --bg:#f3f6fa;
    --success:#13795b; --success-soft:#e8f7f1; --danger:#dc2626; --danger-deep:#b91c1c;
    --danger-soft:#fee2e2; --warn:#a35f00; --warn-soft:#fff7e6;
    --shadow:0 18px 50px rgba(18,44,84,.11); --radius:18px;
  }
  *{ box-sizing:border-box; }
  .omr-report{ margin:0; min-height:100vh; color:var(--ink); background:var(--bg);
    font-family:Pretendard,"Pretendard Variable","Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",Arial,sans-serif;
    line-height:1.55; -webkit-font-smoothing:antialiased;
    padding:22px 14px 48px; font-variant-numeric:tabular-nums; }
  .doc{ width:min(760px,100%); margin:0 auto; background:#fff; border-radius:6px;
    box-shadow:var(--shadow); overflow:hidden; border-top:8px solid var(--navy); }
  .hero{ padding:24px 26px 0; }
  .brand{ display:flex; align-items:center; gap:12px; }
  .brand-mark{ width:42px; height:42px; border-radius:13px; display:grid; place-items:center;
    background:linear-gradient(145deg,var(--navy),var(--blue)); color:#fff; font-weight:900;
    font-size:20px; box-shadow:0 8px 18px rgba(24,60,115,.22); }
  .brand strong{ font-size:16px; letter-spacing:-.02em; display:block; }
  .brand span{ color:var(--muted); font-size:12px; }
  .title-block{ margin:20px 0 16px; }
  .title-block .eyebrow{ margin:0 0 5px; color:var(--blue); font-weight:850; font-size:11px; letter-spacing:.13em; }
  .title-block h1{ margin:0; font-size:26px; letter-spacing:-.04em; line-height:1.2; text-wrap:balance; }
  .title-block .band{ display:inline-block; margin-top:10px; padding:4px 13px; border-radius:99px;
    font-size:12px; font-weight:800; }
  .strip{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr; background:var(--navy);
    color:#fff; border-radius:12px 12px 0 0; overflow:hidden; }
  .strip>div{ padding:14px 16px; border-right:1px solid rgba(255,255,255,.15); }
  .strip>div:last-child{ border-right:0; }
  .strip span{ display:block; color:#c8d9ec; font-size:11px; }
  .strip strong{ display:block; margin-top:3px; font-size:15px; letter-spacing:-.02em; }
  .body{ padding:24px 26px 28px; display:flex; flex-direction:column; gap:18px; }
  .review{ padding:13px 16px; border-radius:12px; background:var(--warn-soft);
    border:1px solid #f4dca6; color:#6f4a0a; font-size:13px; }
  .review strong{ color:var(--warn); }
  .kpis{ display:grid; grid-template-columns:1.3fr 1fr 1fr 1fr; border:1px solid #d9e2ed;
    border-radius:13px; overflow:hidden; }
  .kpis>div{ padding:16px; border-right:1px solid #e2e8f0; min-height:88px; }
  .kpis>div:last-child{ border-right:0; }
  .kpis span{ display:block; color:var(--muted); font-size:11px; }
  .kpis strong{ display:block; margin-top:5px; font-size:22px; color:var(--navy); letter-spacing:-.03em; }
  .kpis small{ color:var(--muted); font-size:10px; }
  .kpis .emph{ background:var(--navy); color:#fff; }
  .kpis .emph span,.kpis .emph small{ color:#c8d8eb; }
  .kpis .emph strong{ display:inline; color:#fff; font-size:36px; }
  .kpis .emph small{ margin-left:3px; }
  .panel{ border:1px solid #dce4ee; border-radius:13px; padding:18px; background:#fff; }
  .panel-title{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:14px; }
  .panel-title h4{ margin:0; font-size:15px; letter-spacing:-.02em; }
  .panel-title span{ color:var(--muted); font-size:12px; }
  .bar-row+.bar-row{ margin-top:12px; }
  .bar-label{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:5px; font-size:12px; }
  .bar-label strong{ font-weight:700; }
  .bar-label span{ color:var(--muted); }
  .bar-track{ height:9px; background:#e9eef5; border-radius:99px; overflow:hidden; }
  .bar-track i{ height:100%; display:block; border-radius:inherit;
    background:linear-gradient(90deg,var(--navy),var(--blue)); }
  .bar-track.avg i{ background:linear-gradient(90deg,#9aa8bd,#c2ccdb); }
  .qgrid{ display:grid; grid-template-columns:repeat(auto-fill,minmax(52px,1fr)); gap:6px; }
  .qcell{ min-height:48px; border:1px solid #dce3ec; border-radius:8px; display:flex;
    flex-direction:column; align-items:center; justify-content:center; }
  .qcell span{ font-size:10px; color:var(--muted); }
  .qcell strong{ font-size:18px; line-height:1.1; }
  .qcell.correct{ background:var(--success-soft); border-color:#b9e4d5; color:var(--success); }
  .qcell.correct span{ color:#4f9e86; }
  .qcell.wrong{ background:var(--danger); border-color:var(--danger-deep); color:#fff;
    box-shadow:0 2px 7px rgba(185,28,28,.22); }
  .qcell.wrong span{ color:#fee2e2; }
  .qcell.blank{ background:#f4f5f7; color:#98a2b3; }
  .legend{ font-size:11px; color:var(--muted); margin-top:12px; display:flex; gap:6px;
    flex-wrap:wrap; justify-content:space-between; }
  .footer{ padding:18px 26px; border-top:1px solid var(--line); background:#fafbfd;
    display:flex; align-items:center; gap:14px; }
  .footer .brand-mark{ width:30px; height:30px; border-radius:9px; font-size:15px; }
  .footer strong{ font-size:13px; }
  .footer p{ margin:0; color:var(--muted); font-size:11px; }
  @media (max-width:560px){
    .hero{ padding:20px 16px 0; }
    .body{ padding:18px 16px 22px; }
    .title-block h1{ font-size:22px; }
    .strip{ grid-template-columns:1fr 1fr; }
    .strip>div:nth-child(2){ border-right:0; }
    .strip>div:nth-child(-n+2){ border-bottom:1px solid rgba(255,255,255,.15); }
    .kpis{ grid-template-columns:1fr 1fr; }
    .kpis>div{ border-right:1px solid #e2e8f0 !important; border-bottom:1px solid #e2e8f0; }
    .kpis>div:nth-child(2n){ border-right:0 !important; }
    .kpis .emph{ grid-column:1 / -1; }
  }
"""


def _grade_band(percentile: float) -> tuple:
    """백분위 → (성취 수준 라벨, 글자색, 배경색)."""
    if percentile >= 89:
        return "최상위권", "#0f5f47", "var(--success-soft)"
    if percentile >= 60:
        return "상위권", "#1b3f78", "var(--blue-soft)"
    if percentile >= 40:
        return "중위권", "#8a5a00", "var(--gold-soft)"
    return "집중 보완 권장", "#9a2020", "var(--danger-soft)"


def _bar(label: str, value: float, total: float, detail: str, avg: bool = False) -> str:
    pct = max(0.0, min(100.0, (value / total * 100) if total else 0))
    cls = "bar-track avg" if avg else "bar-track"
    return (f'<div class="bar-row"><div class="bar-label"><strong>{html.escape(label)}</strong>'
            f'<span>{html.escape(detail)}</span></div>'
            f'<div class="{cls}"><i style="width:{pct:.1f}%"></i></div></div>')


def _question_cells(questions: list) -> str:
    sym = {"correct": "○", "wrong": "×", "blank": "–"}
    cells = []
    for q in questions:
        mine = q["mine"] if q["mine"] is not None else "무응답"
        tip = f'{q["no"]}번 · 내 답 {mine} / 정답 {q["correct"]} · {q["point"]:g}점'
        cells.append(
            f'<div class="qcell {q["status"]}" title="{html.escape(tip)}">'
            f'<span>{q["no"]}</span><strong>{sym[q["status"]]}</strong></div>'
        )
    return "".join(cells)


def render_report_body(meta: ExamMeta, student: dict, result: dict,
                       questions: list, review_flags: list) -> str:
    """성적표 본문(.omr-report 래퍼)을 반환. 단독 HTML과 아티팩트가 함께 사용."""
    band, band_ink, band_bg = _grade_band(result["percentile"])
    name = html.escape(student.get("name") or "")
    sid = html.escape(str(student.get("student_id") or ""))
    title = html.escape(meta.title)
    school = html.escape(meta.school) or "OMR 채점 리포트"
    date = html.escape(meta.date) or ""
    mark = (student.get("name") or school or "R")[0]

    review_html = ""
    if review_flags:
        items = ", ".join(
            (f"{f.get('no','')}번" if f.get("type") == "question" else f"학번 {f.get('col', 0) + 1}자리")
            + f"({'중복표기' if f['status'] == 'multiple' else '무응답'})"
            for f in review_flags
        )
        review_html = (
            f'<div class="review"><strong>⚠️ 판독 확인</strong> — 아래 항목은 마킹이 '
            f'모호하여 담당 교사가 직접 검수했습니다: {html.escape(items)}</div>'
        )

    bars = (
        _bar("학생 점수", result["raw_score"], result["total_points"],
             f'{result["raw_score"]:g}점') +
        _bar("반 평균", result["class_mean"], result["total_points"],
             f'{result["class_mean"]:g}점 · 표준편차 {result["class_std"]:g}', avg=True)
    )
    qcells = _question_cells(questions)

    return f"""<div class="omr-report"><article class="doc">
  <header class="hero">
    <div class="brand">
      <div class="brand-mark">{html.escape(mark)}</div>
      <div><strong>{school}</strong><span>OMR 정밀 채점 리포트</span></div>
    </div>
    <div class="title-block">
      <p class="eyebrow">STUDENT SCORE REPORT</p>
      <h1>{title}</h1>
      <span class="band" style="color:{band_ink};background:{band_bg}">{band} · 백분위 {result['percentile']:g}</span>
    </div>
    <div class="strip">
      <div><span>학생명</span><strong>{name or '—'}</strong></div>
      <div><span>학번</span><strong>{sid or '—'}</strong></div>
      <div><span>응시 인원</span><strong>{result['class_size']}명</strong></div>
      <div><span>발행일</span><strong>{date or '—'}</strong></div>
    </div>
  </header>

  <div class="body">
    {review_html}

    <div class="kpis">
      <div class="emph"><span>원점수</span><strong>{result['raw_score']:g}</strong><small>/ {result['total_points']:g}점</small></div>
      <div><span>학원 내 석차</span><strong>{result['rank']}위</strong><small>{result['class_size']}명 중</small></div>
      <div><span>전체 백분위</span><strong>{result['percentile']:g}</strong><small>상위 추정</small></div>
      <div><span>정답 문항</span><strong>{result['num_correct']}개</strong><small>{len(questions)}문항 중</small></div>
    </div>

    <section class="panel">
      <div class="panel-title"><h4>점수 비교</h4><span>{result['total_points']:g}점 만점</span></div>
      {bars}
    </section>

    <section class="panel">
      <div class="panel-title"><h4>문항별 정오답</h4><span>○ 정답 · × 오답 · – 미입력</span></div>
      <div class="qgrid">{qcells}</div>
      <div class="legend"><span>마우스를 올리면 문항별 내 답·정답·배점을 볼 수 있습니다.</span>
        <span>정답 {result['num_correct']} · 오답 {result['num_wrong']} · 미입력 {result['num_blank']}</span></div>
    </section>
  </div>

  <footer class="footer">
    <div class="brand-mark">{html.escape(mark)}</div>
    <div><strong>{school}</strong>
      <p>본 성적표는 개인 열람용 링크입니다. 백분위는 응시 집단 기준 참고값이며 링크 공유에 유의해 주세요.</p></div>
  </footer>
</article></div>"""


def render_report_html(meta: ExamMeta, student: dict, result: dict,
                       questions: list, review_flags: list) -> str:
    """단독 열람용 완성 HTML 문서."""
    body = render_report_body(meta, student, result, questions, review_flags)
    title = html.escape(meta.title)
    name = html.escape(student.get("name") or "")
    return f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>{title} 성적표 - {name}</title>
<style>{REPORT_CSS}</style></head>
<body>{body}</body></html>"""


# ----------------------------------------------------------------------------
# 상위 API
# ----------------------------------------------------------------------------
def build_reports(records: list, key: AnswerKey, meta: ExamMeta, out_dir: str,
                  base_url: str = "", salt: str = DEFAULT_SALT) -> dict:
    """응시자별 HTML 성적표 + manifest(JSON)를 생성한다.

    records: [{"student_id","name","answers"(q->1based/None),"review_flags"?}]
    base_url: 링크 접두사 (예: "https://reports.school.kr/mid2026").
              지정 시 manifest의 url이 절대경로가 되어 알림톡에 바로 사용 가능.
    반환: {"dir","index","manifest","entries":[{student_id,name,token,file,url,score,rank}]}
    """
    reports_dir = os.path.join(out_dir, "reports", meta.exam_id)
    os.makedirs(reports_dir, exist_ok=True)

    scored = score_batch(records, key)
    scored_by = {s["student_id"]: s for s in scored}

    entries = []
    for rec in records:
        sid = rec["student_id"]
        s = scored_by[sid]
        detail = score_one(rec["answers"], key)
        questions = []
        for q in sorted(key.answers):
            got = rec["answers"].get(q)
            if got is None:
                status = "blank"
            elif got == key.answers[q]:
                status = "correct"
            else:
                status = "wrong"
            questions.append({"no": q, "mine": got, "correct": key.answers[q],
                              "status": status, "point": key.points[q]})

        token = make_token(salt, meta.exam_id, sid)
        fname = f"{token}.html"
        student = {"student_id": sid, "name": rec.get("name", "")}
        result = {**s, **detail}
        htmldoc = render_report_html(meta, student, result, questions,
                                     rec.get("review_flags", []))
        with open(os.path.join(reports_dir, fname), "w", encoding="utf-8") as fp:
            fp.write(htmldoc)

        url = f"{base_url.rstrip('/')}/{fname}" if base_url else fname
        entries.append({
            "student_id": sid, "name": rec.get("name", ""),
            "token": token, "file": fname, "url": url,
            "score": s["raw_score"], "total": s["total_points"],
            "rank": s["rank"], "class_size": s["class_size"],
            "percentile": s["percentile"],
        })

    # manifest: 알림톡 발송 단계의 입력이 된다
    manifest = {
        "exam": {"exam_id": meta.exam_id, "title": meta.title,
                 "date": meta.date, "school": meta.school},
        "base_url": base_url,
        "count": len(entries),
        "entries": entries,
    }
    manifest_path = os.path.join(reports_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)

    # index.html: 교사용 전체 링크 목록
    index_path = os.path.join(reports_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as fp:
        fp.write(_render_index(meta, entries, base_url))

    return {"dir": reports_dir, "index": index_path,
            "manifest": manifest_path, "entries": entries}


def _render_index(meta: ExamMeta, entries: list, base_url: str) -> str:
    rows = "".join(
        f'<tr><td>{e["rank"]}</td><td>{html.escape(e["name"])}</td>'
        f'<td>{html.escape(e["student_id"])}</td><td>{e["score"]:g}</td>'
        f'<td><a href="{html.escape(e["file"])}" target="_blank">열람</a></td></tr>'
        for e in sorted(entries, key=lambda x: x["rank"])
    )
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(meta.title)} 성적표 링크 목록</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:0 auto;padding:20px}}
table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left}}
th{{color:#64748b;font-size:13px}}a{{color:#2563eb}}</style></head>
<body><h2>{html.escape(meta.title)}</h2>
<p style="color:#64748b">{html.escape(meta.school)} · {html.escape(meta.date)} · 응시 {len(entries)}명</p>
<table><tr><th>석차</th><th>이름</th><th>학번</th><th>점수</th><th>링크</th></tr>{rows}</table>
</body></html>"""
