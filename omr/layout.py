"""OMR 시트 기하 배치 계산 (생성기·판독기 공용).

좌표 규약: 단위 mm, 원점은 좌상단, x는 오른쪽, y는 아래 방향.
ReportLab(좌하단 원점)·PIL(좌상단 원점) 렌더러가 각자 변환한다.

판독기는 4개 ArUco 마커의 '중심'이 이루는 사각형을 기준 좌표계로 삼는다.
따라서 각 버블의 위치는 이 기준 사각형에 대한 정규화 좌표(u, v)로도 저장되어,
스캔 시 시트가 기울거나 크기가 달라도 동일하게 매핑된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

# A4 규격 (mm)
A4_W_MM = 210.0
A4_H_MM = 297.0

# ArUco 마커: 4X4_50 사전, 코너별 고유 ID → 방향 판별이 명확
MARKER_DICT = "DICT_4X4_50"
MARKER_IDS = {"TL": 0, "TR": 1, "BR": 2, "BL": 3}


@dataclass
class SheetConfig:
    """한 시험지의 논리 구성."""

    exam_id: str = "EXAM"
    title: str = "OMR 답안지"
    num_questions: int = 40
    num_choices: int = 5          # 보기 수 (①~⑤)
    id_digits: int = 8            # 학번(수험번호) 자릿수
    questions_per_column: int = 20  # 문항 열당 문항 수
    style: str = "basic"          # "basic"(세로 단순형) | "exam"(수능형 가로)
    period: str = ""              # 교시 표기 (예: "3")
    subject_label: str = ""       # 영역 표기 (예: "영어 영역")
    academy: str = ""             # 학원명 (구석 로고 옆)
    academy_logo: str = ""        # 학원 로고 이미지 경로(있으면 우하단에 배치)


@dataclass
class Bubble:
    """단일 마킹 버블. 위치는 mm와 정규화(u, v) 좌표를 함께 보유."""

    role: Literal["question", "id"]
    index: int          # question: 문항번호(1-base) / id: 자릿수 열(0-base)
    value: int          # question: 보기 인덱스(0-base) / id: 숫자값(0-9)
    x_mm: float
    y_mm: float
    u: float = 0.0
    v: float = 0.0


@dataclass
class Layout:
    """렌더러·판독기가 소비하는 완성된 배치 정보."""

    config: SheetConfig
    page_w_mm: float
    page_h_mm: float
    marker_size_mm: float
    marker_centers_mm: dict          # {"TL": (x, y), ...}
    bubble_radius_mm: float
    bubbles: list[Bubble]
    # 기준 사각형(마커 중심 사각형)의 경계
    ref_left_mm: float
    ref_right_mm: float
    ref_top_mm: float
    ref_bottom_mm: float
    # 정보 요소 위치
    title_xy_mm: tuple
    qr_xy_mm: tuple                  # QR 좌상단
    qr_size_mm: float
    id_origin_mm: tuple              # 학번 그리드 좌상단(첫 버블 중심)
    q_columns_origin_mm: list        # 각 문항 열의 시작 좌표
    id_col_pitch_mm: float = 8.0     # 학번 열 간격(렌더러용)
    id_row_pitch_mm: float = 5.0     # 학번 행 간격
    q_choice_pitch_mm: float = 7.0   # 보기 버블 간격
    q_row_pitch_mm: float = 8.0      # 문항 행 간격
    style: str = "basic"

    def template_dict(self, dpi: int) -> dict:
        """판독기가 사용할 템플릿(JSON 직렬화용)."""
        return {
            "exam_id": self.config.exam_id,
            "title": self.config.title,
            "num_questions": self.config.num_questions,
            "num_choices": self.config.num_choices,
            "id_digits": self.config.id_digits,
            "marker_dict": MARKER_DICT,
            "marker_ids": MARKER_IDS,
            "render_dpi": dpi,
            "ref_w_mm": self.ref_right_mm - self.ref_left_mm,
            "ref_h_mm": self.ref_bottom_mm - self.ref_top_mm,
            "bubble_radius_mm": self.bubble_radius_mm,
            "bubbles": [
                {
                    "role": b.role,
                    "index": b.index,
                    "value": b.value,
                    "u": round(b.u, 6),
                    "v": round(b.v, 6),
                }
                for b in self.bubbles
            ],
        }


def build_layout(config: SheetConfig) -> Layout:
    """SheetConfig로부터 구체 배치를 계산한다."""
    if config.style == "exam":
        return build_exam_layout(config)
    page_w, page_h = A4_W_MM, A4_H_MM
    margin = 12.0
    marker_size = 14.0

    # 마커 중심: 페이지 네 모서리 안쪽. 방향은 ID로 구분하므로 대칭이어도 무방.
    m_left = margin + marker_size / 2
    m_right = page_w - margin - marker_size / 2
    m_top = margin + marker_size / 2
    m_bottom = page_h - margin - marker_size / 2
    marker_centers = {
        "TL": (m_left, m_top),
        "TR": (m_right, m_top),
        "BR": (m_right, m_bottom),
        "BL": (m_left, m_bottom),
    }

    ref_left, ref_right = m_left, m_right
    ref_top, ref_bottom = m_top, m_bottom
    ref_w = ref_right - ref_left
    ref_h = ref_bottom - ref_top

    def norm(x: float, y: float) -> tuple:
        return (x - ref_left) / ref_w, (y - ref_top) / ref_h

    bubble_radius = 2.6
    bubbles: list[Bubble] = []

    # --- 상단 정보 영역 ---
    title_xy = (page_w / 2, m_top + 6)
    qr_size = 22.0
    qr_xy = (m_right - qr_size, m_top + 6)  # 우상단 QR 좌상단

    # --- 학번(수험번호) 그리드 ---
    # 각 자릿수 = 세로 열(0~9). 열은 좌→우로 배치.
    id_top = m_top + 34
    id_left = m_left + 8
    id_col_pitch = 8.0
    id_row_pitch = 5.0
    id_origin = (id_left, id_top)
    for col in range(config.id_digits):
        cx = id_left + col * id_col_pitch
        for digit in range(10):
            cy = id_top + digit * id_row_pitch
            u, v = norm(cx, cy)
            bubbles.append(Bubble("id", col, digit, cx, cy, u, v))

    # --- 문항 영역 ---
    n = config.num_questions
    per_col = config.questions_per_column
    num_cols = (n + per_col - 1) // per_col
    q_top = id_top + 10 * id_row_pitch + 12
    q_area_left = m_left + 6
    q_area_right = m_right - 6
    col_pitch = (q_area_right - q_area_left) / num_cols
    choice_pitch = 7.0
    label_gap = 12.0  # 문항번호 라벨과 첫 보기 사이 간격

    # 모든 문항 버블이 기준 사각형(마커 안쪽)에 들어오도록 행 간격을 적응 계산.
    # 마지막 행이 하단 마커 위 안전 여백(6mm) 안에 위치하게 한다.
    q_bottom_limit = ref_bottom - 6.0
    rows_in_col = min(per_col, n)
    if rows_in_col > 1:
        row_pitch = min(8.2, (q_bottom_limit - q_top) / (rows_in_col - 1))
    else:
        row_pitch = 8.2

    q_columns_origin = []
    for q in range(1, n + 1):
        col_i = (q - 1) // per_col
        row_i = (q - 1) % per_col
        col_x0 = q_area_left + col_i * col_pitch
        row_y = q_top + row_i * row_pitch
        if row_i == 0:
            q_columns_origin.append((col_x0, q_top))
        first_choice_x = col_x0 + label_gap
        for ci in range(config.num_choices):
            cx = first_choice_x + ci * choice_pitch
            cy = row_y
            u, v = norm(cx, cy)
            bubbles.append(Bubble("question", q, ci, cx, cy, u, v))

    return Layout(
        config=config,
        page_w_mm=page_w,
        page_h_mm=page_h,
        marker_size_mm=marker_size,
        marker_centers_mm=marker_centers,
        bubble_radius_mm=bubble_radius,
        bubbles=bubbles,
        ref_left_mm=ref_left,
        ref_right_mm=ref_right,
        ref_top_mm=ref_top,
        ref_bottom_mm=ref_bottom,
        title_xy_mm=title_xy,
        qr_xy_mm=qr_xy,
        qr_size_mm=qr_size,
        id_origin_mm=id_origin,
        q_columns_origin_mm=q_columns_origin,
        id_col_pitch_mm=id_col_pitch,
        id_row_pitch_mm=id_row_pitch,
        q_choice_pitch_mm=choice_pitch,
        q_row_pitch_mm=row_pitch,
        style="basic",
    )


def build_exam_layout(config: SheetConfig) -> Layout:
    """수능형 가로 답안지 배치. 성명·수험번호(좌측정렬) + 3단 문항.

    판독 관점에서는 기본형과 동일하게 ArUco 마커 기준 사각형 + 버블 정규화 좌표를
    저장하므로 reader가 그대로 동작한다. 나머지는 렌더러가 수능풍으로 그린다.
    """
    page_w, page_h = A4_H_MM, A4_W_MM   # landscape (297 x 210)
    margin = 10.0
    marker_size = 11.0

    m_left = margin + marker_size / 2
    m_right = page_w - margin - marker_size / 2
    m_top = margin + marker_size / 2
    m_bottom = page_h - margin - marker_size / 2
    marker_centers = {
        "TL": (m_left, m_top), "TR": (m_right, m_top),
        "BR": (m_right, m_bottom), "BL": (m_left, m_bottom),
    }
    ref_left, ref_right = m_left, m_right
    ref_top, ref_bottom = m_top, m_bottom
    ref_w, ref_h = ref_right - ref_left, ref_bottom - ref_top

    def norm(x, y):
        return (x - ref_left) / ref_w, (y - ref_top) / ref_h

    bubble_radius = 2.5
    bubbles: list[Bubble] = []

    title_xy = (m_left + 16, m_top + 4)
    qr_size = 18.0
    qr_xy = (m_right - 15 - qr_size, m_top + 1)   # TR 마커와 겹치지 않게 왼쪽으로

    # --- 좌측 패널: 수험번호 그리드 (4~5자리, 왼쪽부터 채워쓰기) ---
    # 성명·학교/학년·수강반 박스 아래로 충분히 내려 간섭 제거.
    id_left = m_left + 12
    id_top = m_top + 91
    id_col_pitch = 9.0
    id_row_pitch = 6.4
    id_origin = (id_left, id_top)
    for col in range(config.id_digits):
        cx = id_left + col * id_col_pitch
        for digit in range(10):
            cy = id_top + digit * id_row_pitch
            u, v = norm(cx, cy)
            bubbles.append(Bubble("id", col, digit, cx, cy, u, v))

    # --- 우측: 3단 문항 (수능식 20/20/5 등) ---
    n = config.num_questions
    per_col = config.questions_per_column or 20
    num_cols = (n + per_col - 1) // per_col
    q_area_left = m_left + 78     # 좌측 패널 오른쪽부터
    q_area_right = m_right - 4
    col_pitch = (q_area_right - q_area_left) / num_cols
    choice_pitch = 6.6
    label_gap = 13.0
    q_top = m_top + 37           # 헤더행(문번/답란)과 1·21·41번 간섭 방지
    q_bottom_limit = m_bottom - 9  # 최하단 행이 가장자리 왜곡 영역에 닿지 않도록 여유
    rows = min(per_col, n)
    row_pitch = min(9.5, (q_bottom_limit - q_top) / (rows - 1)) if rows > 1 else 9.5

    q_columns_origin = []
    for q in range(1, n + 1):
        col_i = (q - 1) // per_col
        row_i = (q - 1) % per_col
        col_x0 = q_area_left + col_i * col_pitch
        row_y = q_top + row_i * row_pitch
        if row_i == 0:
            q_columns_origin.append((col_x0, q_top))
        first_choice_x = col_x0 + label_gap
        for ci in range(config.num_choices):
            cx = first_choice_x + ci * choice_pitch
            u, v = norm(cx, row_y)
            bubbles.append(Bubble("question", q, ci, cx, row_y, u, v))

    return Layout(
        config=config,
        page_w_mm=page_w, page_h_mm=page_h,
        marker_size_mm=marker_size, marker_centers_mm=marker_centers,
        bubble_radius_mm=bubble_radius, bubbles=bubbles,
        ref_left_mm=ref_left, ref_right_mm=ref_right,
        ref_top_mm=ref_top, ref_bottom_mm=ref_bottom,
        title_xy_mm=title_xy, qr_xy_mm=qr_xy, qr_size_mm=qr_size,
        id_origin_mm=id_origin, q_columns_origin_mm=q_columns_origin,
        id_col_pitch_mm=id_col_pitch, id_row_pitch_mm=id_row_pitch,
        q_choice_pitch_mm=choice_pitch, q_row_pitch_mm=row_pitch,
        style="exam",
    )
