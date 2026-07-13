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
# ----------------------------------------------------------------------------
def _grade_band(percentile: float) -> tuple:
    """백분위 → (등급 라벨, 색)."""
    if percentile >= 89:
        return "상위권", "#16a34a"
    if percentile >= 60:
        return "중상위권", "#0284c7"
    if percentile >= 40:
        return "중위권", "#ca8a04"
    return "노력 필요", "#dc2626"


def _distribution_svg(raw: float, mean: float, std: float, total: float) -> str:
    """내 점수 vs 반 평균을 보여주는 간단한 막대 SVG."""
    w, h = 320, 96
    pad = 8
    bw = w - 2 * pad

    def x(v):
        return pad + bw * (max(0.0, min(1.0, v / total if total else 0)))

    my_x = x(raw)
    mean_x = x(mean)
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:360px">
  <rect x="{pad}" y="40" width="{bw}" height="14" rx="7" fill="#e5e7eb"/>
  <rect x="{pad}" y="40" width="{my_x-pad}" height="14" rx="7" fill="#2563eb"/>
  <line x1="{mean_x}" y1="32" x2="{mean_x}" y2="62" stroke="#dc2626" stroke-width="2" stroke-dasharray="3 2"/>
  <text x="{my_x}" y="30" font-size="11" fill="#2563eb" text-anchor="middle">내 {raw:g}</text>
  <text x="{mean_x}" y="78" font-size="11" fill="#dc2626" text-anchor="middle">평균 {mean:g}</text>
  <text x="{pad}" y="78" font-size="10" fill="#9ca3af">0</text>
  <text x="{w-pad}" y="78" font-size="10" fill="#9ca3af" text-anchor="end">{total:g}</text>
</svg>"""


def _question_cells(questions: list) -> str:
    cells = []
    sym = {"correct": ("O", "#16a34a", "#dcfce7"),
           "wrong": ("X", "#dc2626", "#fee2e2"),
           "blank": ("–", "#6b7280", "#f3f4f6")}
    for q in questions:
        s, color, bg = sym[q["status"]]
        mine = q["mine"] if q["mine"] is not None else "무응답"
        cells.append(
            f'<div class="qcell" style="background:{bg}">'
            f'<div class="qno">{q["no"]}</div>'
            f'<div class="qmark" style="color:{color}">{s}</div>'
            f'<div class="qdet">내 {mine} / 정답 {q["correct"]}</div>'
            f'</div>'
        )
    return "".join(cells)


def render_report_html(meta: ExamMeta, student: dict, result: dict,
                       questions: list, review_flags: list) -> str:
    band, band_color = _grade_band(result["percentile"])
    name = html.escape(student.get("name") or "")
    sid = html.escape(str(student.get("student_id") or ""))
    review_html = ""
    if review_flags:
        items = ", ".join(
            (f"{f.get('no','')}번" if f.get("type") == "question" else f"학번 {f.get('col','')+1}자리")
            + f"({'중복표기' if f['status']=='multiple' else '무응답'})"
            for f in review_flags
        )
        review_html = (
            f'<div class="review">⚠️ 판독 시 확인이 필요한 항목이 있어 담당 교사가 검수했습니다: {items}</div>'
        )

    dist = _distribution_svg(result["raw_score"], result["class_mean"],
                             result["class_std"], result["total_points"])
    qcells = _question_cells(questions)
    title = html.escape(meta.title)
    school = html.escape(meta.school)
    date = html.escape(meta.date)

    return f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>{title} 성적표 - {name}</title>
<style>
  :root {{ --bg:#f8fafc; --card:#ffffff; --ink:#0f172a; --sub:#64748b; --line:#e2e8f0; --brand:#2563eb; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b1220; --card:#111a2e; --ink:#e5edff; --sub:#94a3b8; --line:#1e293b; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;
    line-height:1.5; -webkit-text-size-adjust:100%; }}
  .wrap {{ max-width:520px; margin:0 auto; padding:16px 14px 40px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:18px 16px; margin-bottom:14px; }}
  .head .school {{ font-size:13px; color:var(--sub); }}
  .head h1 {{ font-size:20px; margin:2px 0 4px; }}
  .head .date {{ font-size:12px; color:var(--sub); }}
  .who {{ display:flex; justify-content:space-between; align-items:baseline; margin-top:10px; }}
  .who .name {{ font-size:18px; font-weight:700; }}
  .who .sid {{ font-size:13px; color:var(--sub); }}
  .score {{ text-align:center; padding:10px 0 4px; }}
  .score .big {{ font-size:52px; font-weight:800; color:var(--brand); line-height:1; }}
  .score .big small {{ font-size:20px; color:var(--sub); font-weight:600; }}
  .band {{ display:inline-block; margin-top:8px; padding:3px 12px; border-radius:999px;
    color:#fff; font-size:13px; font-weight:700; }}
  .stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:14px; }}
  .stat {{ background:var(--bg); border-radius:12px; padding:10px 6px; text-align:center; }}
  .stat b {{ display:block; font-size:19px; }}
  .stat span {{ font-size:11px; color:var(--sub); }}
  .sec-title {{ font-size:14px; font-weight:700; margin:2px 0 10px; }}
  .qgrid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(84px,1fr)); gap:6px; }}
  .qcell {{ border-radius:10px; padding:6px 4px; text-align:center; }}
  .qcell .qno {{ font-size:11px; color:var(--sub); }}
  .qcell .qmark {{ font-size:20px; font-weight:800; line-height:1.1; }}
  .qcell .qdet {{ font-size:10px; color:var(--sub); }}
  .review {{ background:#fffbeb; color:#92400e; border:1px solid #fde68a; border-radius:12px;
    padding:10px 12px; font-size:13px; margin-bottom:14px; }}
  @media (prefers-color-scheme: dark) {{ .review {{ background:#3b2f0b; color:#fde68a; border-color:#78621d; }} }}
  .foot {{ text-align:center; color:var(--sub); font-size:11px; margin-top:6px; }}
  .legend {{ font-size:11px; color:var(--sub); margin-top:8px; }}
</style></head>
<body><div class="wrap">
  <div class="card head">
    <div class="school">{school}</div>
    <h1>{title}</h1>
    <div class="date">{date}</div>
    <div class="who"><span class="name">{name}</span><span class="sid">{sid}</span></div>
  </div>

  {review_html}

  <div class="card">
    <div class="score">
      <div class="big">{result['raw_score']:g}<small> / {result['total_points']:g}</small></div>
      <div class="band" style="background:{band_color}">{band} · 백분위 {result['percentile']:g}</div>
    </div>
    <div class="stats">
      <div class="stat"><b>{result['rank']}등</b><span>석차 (응시 {result['class_size']}명)</span></div>
      <div class="stat"><b>{result['num_correct']}개</b><span>정답 (총 {len(questions)}문항)</span></div>
      <div class="stat"><b>{result['class_mean']:g}</b><span>반 평균 (표준편차 {result['class_std']:g})</span></div>
    </div>
    <div style="margin-top:14px;text-align:center">{dist}</div>
  </div>

  <div class="card">
    <div class="sec-title">문항별 채점 결과</div>
    <div class="qgrid">{qcells}</div>
    <div class="legend">O 정답 · X 오답 · – 무응답 &nbsp;|&nbsp;
      정답 {result['num_correct']} · 오답 {result['num_wrong']} · 무응답 {result['num_blank']}</div>
  </div>

  <div class="foot">본 성적표는 개인 열람용 링크입니다. 링크 공유에 유의해 주세요.</div>
</div></body></html>"""


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
