"""OMR 답안지 생성기.

동일한 Layout을 두 경로로 렌더링한다:
  · PDF  (ReportLab)  — 실제 인쇄용
  · PNG  (PIL)        — 미리보기 및 무(無)스캐너 검증용 기준 이미지

부수 산출물:
  · 판독용 템플릿 JSON (버블 정규화 좌표 + 메타)
  · 시트 식별용 QR(내용: "exam_id|student_id")
  · 코너 정렬용 ArUco 마커 4종
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import asdict

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm as MM
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from .fonts import find_font
from .layout import Layout, SheetConfig, MARKER_DICT, MARKER_IDS, build_layout, layout_fingerprint


# ----------------------------------------------------------------------------
# 마커 / QR
# ----------------------------------------------------------------------------
def _aruco_dict():
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, MARKER_DICT))


def make_marker_image(corner: str, px: int = 300) -> Image.Image:
    """코너별 ArUco 마커를 흰 여백(quiet zone) 포함 PIL 이미지로 반환."""
    d = _aruco_dict()
    marker = cv2.aruco.generateImageMarker(d, MARKER_IDS[corner], px)
    quiet = px // 8
    canvas = np.full((px + 2 * quiet, px + 2 * quiet), 255, np.uint8)
    canvas[quiet:quiet + px, quiet:quiet + px] = marker
    return Image.fromarray(canvas, "L")


def make_qr_image(data: str, px: int = 300) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("L").resize(
        (px, px), Image.NEAREST
    )


# ----------------------------------------------------------------------------
# PDF 렌더러
# ----------------------------------------------------------------------------
def render_pdf(layout: Layout, path: str, student=None, font_path: str | None = None):
    font_path = font_path or find_font()
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as RLTTFont

    font_name = "OMRFont"
    try:
        pdfmetrics.registerFont(RLTTFont(font_name, font_path))
    except Exception:
        font_name = "Helvetica"

    c = rl_canvas.Canvas(path, pagesize=A4)
    ph = layout.page_h_mm

    def X(x_mm):
        return x_mm * MM

    def Y(y_mm):
        return (ph - y_mm) * MM  # top-down → ReportLab bottom-up

    # 마커
    for corner, (cx, cy) in layout.marker_centers_mm.items():
        s = layout.marker_size_mm
        img = ImageReader(make_marker_image(corner))
        c.drawImage(img, X(cx - s / 2), Y(cy + s / 2), width=s * MM, height=s * MM)

    # QR
    exam_id = layout.config.exam_id
    sid = student["id"] if student else ""
    qr_img = ImageReader(make_qr_image(f"{exam_id}|{sid}|{layout_fingerprint(layout.config)}"))
    qx, qy = layout.qr_xy_mm
    qs = layout.qr_size_mm
    c.drawImage(qr_img, X(qx), Y(qy + qs), width=qs * MM, height=qs * MM)

    # 제목 및 안내
    c.setFont(font_name, 15)
    c.drawCentredString(X(layout.title_xy_mm[0]), Y(layout.title_xy_mm[1]), layout.config.title)
    c.setFont(font_name, 8)
    info = f"시험코드(Exam): {exam_id}"
    if student:
        info += f"   응시자: {student.get('name','')} ({sid})"
    c.drawString(X(layout.marker_centers_mm['TL'][0]), Y(layout.title_xy_mm[1] + 8), info)

    # 학번 그리드 헤더
    idx0, idy0 = layout.id_origin_mm
    c.setFont(font_name, 8)
    c.drawString(X(idx0 - 4), Y(idy0 - 7), "학번(수험번호) — 자리별 숫자 마킹")

    # 버블
    r = layout.bubble_radius_mm
    c.setLineWidth(0.5)
    for b in layout.bubbles:
        c.circle(X(b.x_mm), Y(b.y_mm), r * MM, stroke=1, fill=0)
        # 라벨 (버블 내부 소형 숫자)
        c.setFont(font_name, 5)
        label = str(b.value) if b.role == "id" else str(b.value + 1)
        c.drawCentredString(X(b.x_mm), Y(b.y_mm + 1.0), label)

    # 문항 번호 라벨
    c.setFont(font_name, 8)
    n = layout.config.num_questions
    per_col = layout.config.questions_per_column
    for q in range(1, n + 1):
        # 각 문항 첫 버블의 좌측에 번호
        first = next(b for b in layout.bubbles if b.role == "question" and b.index == q and b.value == 0)
        c.drawRightString(X(first.x_mm - 4), Y(first.y_mm + 1.2), str(q))

    c.showPage()
    c.save()


# ----------------------------------------------------------------------------
# PIL 이미지 렌더러 (미리보기 / 검증 기준 이미지)
# ----------------------------------------------------------------------------
def render_image(layout: Layout, dpi: int = 200, student=None, font_path: str | None = None) -> Image.Image:
    font_path = font_path or find_font()

    def mm2px(v):
        return int(round(v / 25.4 * dpi))

    W = mm2px(layout.page_w_mm)
    H = mm2px(layout.page_h_mm)
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    def load_font(pt):
        px = int(round(pt / 72 * dpi))
        try:
            return ImageFont.truetype(font_path, px)
        except Exception:
            return ImageFont.load_default()

    f_title = load_font(15)
    f_info = load_font(8)
    f_small = load_font(5)
    f_q = load_font(8)

    # 마커
    for corner, (cx, cy) in layout.marker_centers_mm.items():
        s = layout.marker_size_mm
        m = make_marker_image(corner).resize((mm2px(s), mm2px(s)), Image.NEAREST).convert("RGB")
        img.paste(m, (mm2px(cx - s / 2), mm2px(cy - s / 2)))

    # QR
    exam_id = layout.config.exam_id
    sid = student["id"] if student else ""
    qs = layout.qr_size_mm
    q = make_qr_image(f"{exam_id}|{sid}|{layout_fingerprint(layout.config)}").resize(
        (mm2px(qs), mm2px(qs)), Image.NEAREST
    ).convert("RGB")
    qx, qy = layout.qr_xy_mm
    img.paste(q, (mm2px(qx), mm2px(qy)))

    # 제목/안내
    _centered_text(draw, mm2px(layout.title_xy_mm[0]), mm2px(layout.title_xy_mm[1]), layout.config.title, f_title)
    info = f"시험코드(Exam): {exam_id}"
    if student:
        info += f"   응시자: {student.get('name','')} ({sid})"
    draw.text((mm2px(layout.marker_centers_mm['TL'][0]), mm2px(layout.title_xy_mm[1] + 4)), info, font=f_info, fill="black")

    idx0, idy0 = layout.id_origin_mm
    draw.text((mm2px(idx0 - 4), mm2px(idy0 - 8)), "학번(수험번호) - 자리별 숫자 마킹", font=f_info, fill="black")

    # 버블
    r = mm2px(layout.bubble_radius_mm)
    for b in layout.bubbles:
        cx, cy = mm2px(b.x_mm), mm2px(b.y_mm)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline="black", width=max(1, dpi // 200))
        label = str(b.value) if b.role == "id" else str(b.value + 1)
        _centered_text(draw, cx, cy, label, f_small, fill=(120, 120, 120))

    # 문항 번호
    for q_no in range(1, layout.config.num_questions + 1):
        first = next(b for b in layout.bubbles if b.role == "question" and b.index == q_no and b.value == 0)
        _right_text(draw, mm2px(first.x_mm - 4), mm2px(first.y_mm), str(q_no), f_q)

    return img


def _centered_text(draw, cx, cy, text, font, fill="black"):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)


def _right_text(draw, rx, cy, text, font, fill="black"):
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((rx - w, cy - h / 2 - bbox[1]), text, font=font, fill=fill)


# ----------------------------------------------------------------------------
# 수능형(exam) 렌더러 — PIL 단일 경로. PDF는 이 이미지를 전면 배치.
# ----------------------------------------------------------------------------
# 색상 (스캔 안정성: 버블 내부는 흰색 유지, 외곽/숫자만 옅은 분홍)
_BG = (255, 255, 255)      # 배경 흰색
_NAVY = (24, 60, 115)
_PINK = (206, 118, 138)
_PINK_TEXT = (146, 58, 80)   # 버블 안 숫자 — 흑백/저품질 인쇄에서도 보이도록 외곽선보다 진하게
_BAND = (238, 241, 246)    # 5행 그룹 음영(연한 회청)
_BORDER = (176, 184, 196)
_INK = (45, 47, 55)


def render_exam_image(layout: Layout, dpi: int = 200, student=None,
                      font_path: str | None = None) -> Image.Image:
    """수능형 가로 답안지를 그린다(성명·수험번호·3단 문항·안내문·학원 로고)."""
    font_path = font_path or find_font()
    cfg = layout.config

    def mm(v):
        return int(round(v / 25.4 * dpi))

    W, H = mm(layout.page_w_mm), mm(layout.page_h_mm)
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)

    def font(pt, bold=False):
        px = int(round(pt / 72 * dpi))
        p = font_path
        if bold:
            cand = font_path.replace("Regular", "Bold").replace("NanumGothic", "NanumGothicBold")
            import os as _os
            if _os.path.exists(cand):
                p = cand
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            return ImageFont.load_default()

    def fit_font(text, max_px, start_pt=12.0, min_pt=7.0, bold=True):
        pt = start_pt
        while pt > min_pt:
            f = font(pt, bold)
            if d.textlength(text, font=f) <= max_px:
                return f
            pt -= 0.5
        return font(min_pt, bold)

    ml, mr = layout.ref_left_mm, layout.ref_right_mm
    mt, mb = layout.ref_top_mm, layout.ref_bottom_mm

    # --- 타이밍 마크 (상·하 가장자리 검은 눈금 — OMR 느낌) ---
    tick_w, tick_h = mm(4.5), mm(2.0)
    x = mm(28)
    while x < W - mm(28):
        d.rectangle([x, mm(3.5), x + tick_w, mm(3.5) + tick_h], fill="black")
        d.rectangle([x, H - mm(3.5) - tick_h, x + tick_w, H - mm(3.5)], fill="black")
        x += mm(9)

    # --- ArUco 정렬 마커 (기능용) ---
    for corner, (cx, cy) in layout.marker_centers_mm.items():
        s = layout.marker_size_mm
        m = make_marker_image(corner).resize((mm(s), mm(s)), Image.NEAREST).convert("RGB")
        img.paste(m, (mm(cx - s / 2), mm(cy - s / 2)))

    # --- QR (우상단) ---
    exam_id = cfg.exam_id
    sid = student["id"] if student else ""
    qs = layout.qr_size_mm
    q = make_qr_image(f"{exam_id}|{sid}|{layout_fingerprint(cfg)}").resize(
        (mm(qs), mm(qs)), Image.NEAREST
    ).convert("RGB")
    qx, qy = layout.qr_xy_mm
    img.paste(q, (mm(qx), mm(qy)))

    # --- 제목 밴드 (좌상단, 코너 마커 오른쪽) ---
    # 답안지 이름 = 사용자가 설정한 시험제목(자동). '답안지'가 없으면 덧붙임.
    # 교시/영역은 값이 있을 때만 표기(영어 모의고사가 아닌 범용 시험은 강제 문구 없음).
    period = (cfg.period or "").strip()
    subject = (cfg.subject_label or "").strip()
    sheet_title = cfg.title or "모의고사"
    if "답안지" not in sheet_title:
        sheet_title = sheet_title + " 답안지"
    has_sub = bool(period or subject)
    # 좌측 패널 공통 좌·우변 (layout.py의 panel_right와 동일식) — 제목 밴드부터
    # 성명/수험번호/감독관 박스까지 전부 이 두 변에 정렬한다.
    idl, idt = layout.id_origin_mm
    cp, rp = layout.id_col_pitch_mm, layout.id_row_pitch_mm
    px0 = mm(ml + 2)
    px1 = max(mm(ml + 54), mm(idl + (cfg.id_digits - 1) * cp + 6))
    # 밴드 상단은 TL 마커(y≈mt+5.5까지) 아래로 내려 마커를 가리지 않게 한다.
    tb_x0, tb_y0 = px0, mm(mt + 8)
    tb_x1, tb_y1 = px1, mm(mt + (28 if has_sub else 21))
    d.rounded_rectangle([tb_x0, tb_y0, tb_x1, tb_y1], radius=mm(2), fill=_NAVY)
    tb_cx = (tb_x0 + tb_x1) // 2
    band_h = tb_y1 - tb_y0

    def _left_mid(x, cy, text, ft, fill="white"):
        bb = d.textbbox((0, 0), text, font=ft)
        d.text((x, cy - (bb[3] - bb[1]) / 2 - bb[1]), text, font=ft, fill=fill)

    if has_sub:
        # 제목은 상단 영역 중앙, 교시/영역 줄은 하단 영역 중앙 — 두 줄을 각각 수직 정렬
        title_cy = tb_y0 + int(band_h * 0.34)
        row_cy = tb_y0 + int(band_h * 0.72)
        _centered_text(d, tb_cx, title_cy, sheet_title,
                       fit_font(sheet_title, tb_x1 - tb_x0 - mm(8), 12, 7), fill="white")
        if period:
            cr = mm(5.0)
            circ_cx = tb_x0 + mm(10)
            d.ellipse([circ_cx - cr, row_cy - cr, circ_cx + cr, row_cy + cr],
                      outline="white", width=mm(0.6))
            _centered_text(d, circ_cx, row_cy, str(period), font(12, True), fill="white")
            label = f"교시   {subject}" if subject else "교시"
            _left_mid(circ_cx + cr + mm(4), row_cy, label,
                      fit_font(label, tb_x1 - (circ_cx + cr + mm(4)) - mm(2), 11, 7))
        else:
            _centered_text(d, tb_cx, row_cy, subject,
                           fit_font(subject, tb_x1 - tb_x0 - mm(8), 11, 7), fill="white")
    else:
        _centered_text(d, tb_cx, (tb_y0 + tb_y1) // 2, sheet_title,
                       fit_font(sheet_title, tb_x1 - tb_x0 - mm(8), 12, 7), fill="white")

    # --- 상단 안내문 (제목 밴드 오른쪽, 밴드 상단에 맞춰 정렬) ---
    ins_x = tb_x1 + mm(8)
    ins_y = mm(mt + 8)
    d.text((ins_x, ins_y), "※ 검은색 컴퓨터용 사인펜만 사용하여 표기하십시오.",
           font=font(8.0), fill=_INK)
    d.text((ins_x, ins_y + mm(5.0)), "※ 수험번호는 왼쪽부터 채워 표기(4~5자리).",
           font=font(8.0), fill=_INK)
    d.text((ins_x, ins_y + mm(10.0)), "※ 한 문항에 하나만 표기 · 수정 시 수정테이프 사용.",
           font=font(8.0), fill=_INK)

    # --- 좌측 인적사항 박스 (성명 / 학교·학년 / 수강반) ---
    # px0/px1은 제목 밴드에서 이미 계산 — 전 좌측 요소가 같은 두 변에 정렬된다.
    def field_box(y0, y1, label, value=""):
        d.rectangle([px0, mm(y0), px1, mm(y1)], outline=_BORDER, width=mm(0.4))
        d.text((px0 + mm(3), mm(y0) + mm(2.2)), label, font=font(9, True), fill=_INK)
        lx = px0 + mm(24)
        d.line([lx, mm(y1) - mm(3.5), px1 - mm(4), mm(y1) - mm(3.5)], fill=_BORDER, width=mm(0.3))
        if value:
            d.text((lx + mm(2), mm(y0) + mm(2.2)), value, font=font(9.5, True), fill=_INK)

    field_box(mt + 30, mt + 42, "성 명", student.get("name", "") if student else "")
    field_box(mt + 43, mt + 55, "학교 · 학년", student.get("school", "") if student else "")
    field_box(mt + 56, mt + 68, "수 강 반", student.get("class", "") if student else "")

    # --- 수험번호 그리드 ---
    box_x0 = px0
    box_x1 = px1
    box_y0 = mm(mt + 71)
    box_y1 = mm(idt + 9 * rp + 5)
    d.rectangle([box_x0, box_y0, box_x1, box_y1], outline=_NAVY, width=mm(0.5))
    # 라벨과 안내문을 같은 줄에 두어 기입칸과 겹치지 않게 함
    d.text((box_x0 + mm(3), box_y0 + mm(2.2)), "수 험 번 호", font=font(9, True), fill=_NAVY)
    d.text((box_x0 + mm(27), box_y0 + mm(3.2)), "(왼쪽부터 4~5자리)", font=font(6.8), fill=_INK)
    # 상단 기입 칸 (안내문 아래, 버블 위 — 상하 모두 이격)
    write_y0 = mm(mt + 80)
    for col in range(cfg.id_digits):
        cx = mm(idl + col * cp)
        d.rectangle([cx - mm(3.2), write_y0, cx + mm(3.2), write_y0 + mm(5.5)],
                    outline=_BORDER, width=mm(0.4))
    # 0~9 버블 (흰 내부 + 분홍 외곽 + 숫자)
    r = mm(layout.bubble_radius_mm)
    bf = font(6.8)
    for b in layout.bubbles:
        if b.role != "id":
            continue
        cx, cy = mm(b.x_mm), mm(b.y_mm)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="white", outline=_PINK, width=mm(0.4))
        _centered_text(d, cx, cy, str(b.value), bf, fill=_PINK_TEXT)

    # --- 감독관 확인 ---
    gv_y0 = box_y1 + mm(4)
    d.rectangle([box_x0, gv_y0, box_x1, gv_y0 + mm(15)], outline=_BORDER, width=mm(0.4))
    d.text((box_x0 + mm(3), gv_y0 + mm(2)), "감독관 확인", font=font(9, True), fill=_INK)
    d.text((box_x0 + mm(3), gv_y0 + mm(7.5)), "(서명 또는 날인)", font=font(7.2), fill=_INK)

    # --- 문항 (다단, 5행 그룹 음영 + 5의 배수 굵게) ---
    per_col = cfg.questions_per_column or 20
    qbycol: dict = {}
    for b in layout.bubbles:
        if b.role != "question":
            continue
        qbycol.setdefault((b.index - 1) // per_col, {}).setdefault(b.index, []).append(b)

    cpitch = layout.q_choice_pitch_mm
    rpitch = layout.q_row_pitch_mm
    cf = font(6.8)
    header_y = mm(mt + 24)   # 첫 문항행(mt+37)과 충분히 이격
    for col_i, qmap in sorted(qbycol.items()):
        qnos = sorted(qmap)
        first_b = sorted(qmap[qnos[0]], key=lambda b: b.value)[0]
        col_left = first_b.x_mm - 13
        col_right = first_b.x_mm + (cfg.num_choices - 1) * cpitch + 4
        num_cx = col_left + 5.5                                   # 문항번호 중심 x
        ans_cx = first_b.x_mm + (cfg.num_choices - 1) * cpitch / 2  # 보기 버블 스팬 중심 x
        # 헤더 바 — '문번'은 번호칸 위, '답란'은 보기칸 위에 정렬
        hy = header_y + mm(3)
        d.rounded_rectangle([mm(col_left), header_y, mm(col_right), header_y + mm(6)],
                            radius=mm(1), fill=_NAVY)
        _centered_text(d, mm(num_cx), hy, "문번", font(8.0, True), fill="white")
        _centered_text(d, mm(ans_cx), hy, "답    란", font(8.0, True), fill="white")
        # 번호칸과 답란 사이 세로 구분선
        d.line([mm(col_left + 11), header_y + mm(1), mm(col_left + 11), header_y + mm(5)],
               fill=(120, 150, 190), width=mm(0.3))
        # 그룹 음영 + 행
        for gi, qno in enumerate(qnos):
            bl = sorted(qmap[qno], key=lambda b: b.value)
            ry = bl[0].y_mm
            band = (gi // 5) % 2 == 1
            if band:
                d.rectangle([mm(col_left), mm(ry - rpitch / 2), mm(col_right), mm(ry + rpitch / 2)],
                            fill=_BAND)
            bold = (qno % 5 == 0)
            _centered_text(d, mm(col_left + 5.5), mm(ry), str(qno),
                           font(9, bold), fill=_NAVY if bold else _INK)
            for b in bl:
                cx, cy = mm(b.x_mm), mm(b.y_mm)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="white", outline=_PINK, width=mm(0.4))
                _centered_text(d, cx, cy, str(b.value + 1), cf, fill=_PINK_TEXT)
        # 열 외곽선
        d.rectangle([mm(col_left), header_y, mm(col_right),
                     mm(sorted(qmap[qnos[-1]], key=lambda b: b.value)[0].y_mm + rpitch / 2)],
                    outline=_BORDER, width=mm(0.4))

    # --- 서술형(주관식) 손기입 칸 (오른쪽 세로 배치) ---
    # 판독 대상 아님: 이 영역에는 버블 좌표가 없어 스캔 판독에 영향을 주지 않는다.
    essay_boxes = getattr(layout, "essay_boxes_mm", None) or []
    if essay_boxes:
        ex0 = essay_boxes[0]["x0"]
        ex1 = essay_boxes[0]["x1"]
        # 영역 헤더 바
        d.rounded_rectangle([mm(ex0), header_y, mm(ex1), header_y + mm(6)],
                            radius=mm(1), fill=_NAVY)
        _centered_text(d, mm((ex0 + ex1) / 2), header_y + mm(3),
                       "서술형 답란 (손으로 작성)", font(8.0, True), fill="white")
        for eb in essay_boxes:
            bx0, by0, bx1, by1 = eb["x0"], eb["y0"], eb["x1"], eb["y1"]
            d.rectangle([mm(bx0), mm(by0), mm(bx1), mm(by1)], outline=_BORDER, width=mm(0.4))
            # 번호 태그(좌상단)
            tag = f'{eb["num"]}'
            d.rounded_rectangle([mm(bx0), mm(by0), mm(bx0 + 11), mm(by0 + 6)],
                                radius=mm(1), fill=_BAND)
            _centered_text(d, mm(bx0 + 5.5), mm(by0 + 3), tag, font(8.5, True), fill=_NAVY)
            d.text((mm(bx0 + 13), mm(by0 + 1.4)), eb.get("label", ""), font=font(7.2), fill=_INK)
            # 옅은 밑줄 가이드(작성 편의)
            gy = by0 + 11
            while gy < by1 - 3:
                d.line([mm(bx0 + 3), mm(gy), mm(bx1 - 3), mm(gy)], fill=(214, 219, 226), width=mm(0.25))
                gy += 7

    # --- 학원 로고 (QR 코드 왼쪽, 답란이 올 수 없는 상단 여백 활용) ---
    academy = cfg.academy or "○○학원"
    logo_path = cfg.academy_logo
    qx, qy = layout.qr_xy_mm
    qs = layout.qr_size_mm
    placed = False
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
            target_h = mm(13)
            target_w = int(logo.width * target_h / logo.height)
            logo = logo.resize((target_w, target_h), Image.LANCZOS)
            bg = Image.new("RGBA", logo.size, (255, 255, 255, 0))
            bg.alpha_composite(logo)
            right_x = mm(qx) - mm(5)                      # QR 왼쪽에 여백 두고 배치
            top_y = mm(qy) + (mm(qs) - target_h) // 2     # QR과 세로 중앙 정렬
            img.paste(bg.convert("RGB"), (right_x - target_w, top_y), bg)
            placed = True
        except Exception:
            placed = False
    if not placed:
        lg = mm(11)
        ac_w = mm(2 + 4.2 * len(academy))
        top_y = mm(qy) + (mm(qs) - lg) // 2
        rx = mm(qx) - mm(5)
        lx = rx - ac_w - lg - mm(2)
        _brand_mark(d, lx, top_y, lg, academy[0], font(6.5, True))
        d.text((lx + lg + mm(2), top_y + lg / 2 - mm(2.4)), academy, font=font(10, True), fill=_NAVY)

    return img


def _brand_mark(d, x, y, size, letter, font_obj):
    d.rounded_rectangle([x, y, x + size, y + size], radius=int(size * 0.28), fill=_NAVY)
    _centered_text(d, x + size // 2, y + size // 2, letter, font_obj, fill="white")


def render_sheet_image(layout: Layout, dpi: int = 200, student=None,
                       font_path: str | None = None) -> Image.Image:
    """스타일에 맞는 답안지 이미지를 반환(basic/exam 공용 진입점)."""
    if layout.config.style == "exam":
        return render_exam_image(layout, dpi=dpi, student=student, font_path=font_path)
    return render_image(layout, dpi=dpi, student=student, font_path=font_path)


def render_pdf_from_image(layout: Layout, path: str, student=None, dpi: int = 300):
    """수능형: 고해상 PIL 이미지를 A4(가로) 전면에 배치한 PDF."""
    im = render_exam_image(layout, dpi=dpi, student=student)
    c = rl_canvas.Canvas(path, pagesize=(layout.page_w_mm * MM, layout.page_h_mm * MM))
    c.drawImage(ImageReader(im), 0, 0, width=layout.page_w_mm * MM, height=layout.page_h_mm * MM)
    c.showPage()
    c.save()


# ----------------------------------------------------------------------------
# 상위 API
# ----------------------------------------------------------------------------
def generate(config: SheetConfig, out_dir: str, dpi: int = 200, students=None,
             make_preview: bool = True) -> dict:
    """PDF·템플릿(JSON)·(선택)미리보기 PNG를 생성한다.

    students: [{"id","name"}, ...] 지정 시 응시자별 시트를 만든다.
    반환: 생성된 산출물 경로 딕셔너리.
    """
    os.makedirs(out_dir, exist_ok=True)
    layout = build_layout(config)

    # 템플릿 JSON (판독기 입력)
    tpl_path = os.path.join(out_dir, f"{config.exam_id}_template.json")
    with open(tpl_path, "w", encoding="utf-8") as fp:
        json.dump(layout.template_dict(dpi), fp, ensure_ascii=False, indent=2)

    result = {"template": tpl_path, "pdfs": [], "previews": []}

    exam_style = config.style == "exam"
    targets = students if students else [None]
    for i, stu in enumerate(targets):
        tag = stu["id"] if stu else "blank"
        pdf_path = os.path.join(out_dir, f"{config.exam_id}_{tag}.pdf")
        if exam_style:
            render_pdf_from_image(layout, pdf_path, student=stu)
        else:
            render_pdf(layout, pdf_path, student=stu)
        result["pdfs"].append(pdf_path)
        if make_preview:
            png_path = os.path.join(out_dir, f"{config.exam_id}_{tag}_preview.png")
            render_sheet_image(layout, dpi=dpi, student=stu).save(png_path)
            result["previews"].append(png_path)

    return result
