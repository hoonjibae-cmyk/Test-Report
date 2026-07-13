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
from .layout import Layout, SheetConfig, MARKER_DICT, MARKER_IDS, build_layout


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
    qr_img = ImageReader(make_qr_image(f"{exam_id}|{sid}"))
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
    q = make_qr_image(f"{exam_id}|{sid}").resize((mm2px(qs), mm2px(qs)), Image.NEAREST).convert("RGB")
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

    targets = students if students else [None]
    for i, stu in enumerate(targets):
        tag = stu["id"] if stu else "blank"
        pdf_path = os.path.join(out_dir, f"{config.exam_id}_{tag}.pdf")
        render_pdf(layout, pdf_path, student=stu)
        result["pdfs"].append(pdf_path)
        if make_preview:
            png_path = os.path.join(out_dir, f"{config.exam_id}_{tag}_preview.png")
            render_image(layout, dpi=dpi, student=stu).save(png_path)
            result["previews"].append(png_path)

    return result
