"""무(無)스캐너 검증 유틸.

생성기가 만든 시트 이미지 위에 지정한 답을 실제 마킹처럼 채우고,
경미한 원근·회전·조명·노이즈를 입혀 '스캔본'을 흉내 낸다.
판독기가 이 이미지를 원래 답으로 복원하는지로 파이프라인을 검증한다.
"""
from __future__ import annotations

import numpy as np
import cv2

from .generator import render_sheet_image
from .layout import Layout, SheetConfig, build_layout


def _mm2px(v, dpi):
    return int(round(v / 25.4 * dpi))


def simulate_marked(config: SheetConfig, answers: dict, student_id: str,
                    dpi: int = 200, distort: bool = True, seed: int = 0) -> np.ndarray:
    """마킹된 스캔본(BGR np.ndarray)을 생성한다.

    answers: {문항no: 1-base 보기번호}
    student_id: 자릿수 문자열 (예: "20250042"); 길이는 config.id_digits 이하.
    """
    layout = build_layout(config)
    pil = render_sheet_image(layout, dpi=dpi)
    img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

    r = int(_mm2px(layout.bubble_radius_mm, dpi) * 0.8)

    def fill(cx_mm, cy_mm):
        cv2.circle(img, (_mm2px(cx_mm, dpi), _mm2px(cy_mm, dpi)), r, (35, 35, 35), -1)

    # 문항 마킹
    bub = {(b.role, b.index, b.value): b for b in layout.bubbles}
    for q, choice in answers.items():
        b = bub.get(("question", q, choice - 1))
        if b:
            fill(b.x_mm, b.y_mm)

    # 학번 마킹: 왼쪽부터 채워쓰기(부족한 자리는 미표기)
    sid = str(student_id)[: config.id_digits]
    for col, ch in enumerate(sid):
        if ch.isdigit():
            b = bub.get(("id", col, int(ch)))
            if b:
                fill(b.x_mm, b.y_mm)

    if distort:
        img = _apply_scan_distortion(img, seed)
    return img


def _apply_scan_distortion(img: np.ndarray, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    h, w = img.shape[:2]
    # 경미한 원근 왜곡: 모서리를 최대 1.2% 이동
    jitter = 0.012
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = (src + rng.uniform(-jitter, jitter, src.shape) * [w, h]).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(img, M, (w, h), borderValue=(255, 255, 255))
    # 경미한 회전
    ang = float(rng.uniform(-1.5, 1.5))
    R = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
    img = cv2.warpAffine(img, R, (w, h), borderValue=(255, 255, 255))
    # 조명 그라디언트 + 가우시안 노이즈
    yy = np.linspace(0.9, 1.05, h)[:, None, None]
    img = np.clip(img.astype(np.float32) * yy, 0, 255).astype(np.uint8)
    noise = rng.normal(0, 5, img.shape).astype(np.float32)
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return img
