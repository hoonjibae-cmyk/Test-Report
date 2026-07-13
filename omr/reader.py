"""OMR 판독기.

파이프라인:
  1. 그레이스케일 로드
  2. ArUco 마커 4개(코너) 검출 → 각 중심 좌표
  3. 마커 중심 사각형 → 정준(canonical) 좌표계로 원근 보정(warp)
  4. 정준 이미지 이진화 후, 템플릿의 각 버블 위치에서 원형 마스크로 채움률 계산
  5. 문항/자릿수별로 채움률을 비교해 마킹 판정 (무응답·중복은 플래그)
  6. QR 디코딩으로 시험코드·응시자 식별

핵심 안전장치: 애매한 마킹은 임의로 정답 처리하지 않고 'review'로 표시한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import cv2
import numpy as np


# 판정 파라미터 (기본값은 스캐너 입력 기준 보수적으로 설정)
@dataclass
class ReadParams:
    canonical_w: int = 1400          # 정준 좌표계 가로 px
    mark_abs_min: float = 0.30       # 마킹으로 인정할 최소 채움률
    ambiguous_ratio: float = 0.70    # 2순위/1순위 비율이 이 값 이상이면 중복 의심


@dataclass
class BubbleReading:
    role: str
    index: int
    value: int
    fill: float


@dataclass
class GroupResult:
    key: int                 # question no 또는 id 자릿수 열
    chosen: int | None       # 선택된 value (question: 0-base 보기 / id: 숫자) / None
    status: str              # "ok" | "blank" | "multiple"
    fills: dict              # {value: fill_ratio}


@dataclass
class ReadResult:
    exam_id: str | None
    student_id_qr: str | None
    student_id_bubbles: str | None
    questions: dict          # {q_no: GroupResult}
    id_columns: dict         # {col: GroupResult}
    review_flags: list       # 사람 검수가 필요한 항목 목록
    warped_shape: tuple

    def answers(self) -> dict:
        """{문항: 1-base 보기번호 또는 None} — 채점기 입력용."""
        out = {}
        for q, g in self.questions.items():
            out[q] = (g.chosen + 1) if (g.status == "ok" and g.chosen is not None) else None
        return out

    def resolved_student_id(self) -> str | None:
        """QR 우선, 없으면 버블에서 조립한 학번."""
        if self.student_id_qr:
            return self.student_id_qr
        return self.student_id_bubbles


def _detect_markers(gray: np.ndarray) -> dict:
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, params)
    corners, ids, _ = detector.detectMarkers(gray)
    centers = {}
    if ids is not None:
        for c, i in zip(corners, ids.flatten()):
            centers[int(i)] = c.reshape(4, 2).mean(axis=0)
    return centers


def _warp_to_canonical(gray: np.ndarray, centers: dict, template: dict, params: ReadParams):
    ids = template["marker_ids"]
    needed = [ids["TL"], ids["TR"], ids["BR"], ids["BL"]]
    missing = [k for k in needed if k not in centers]
    if missing:
        raise ValueError(
            f"정렬 마커를 찾지 못했습니다(누락 ID={missing}). "
            f"스캔 해상도/여백/명암을 확인하세요."
        )
    src = np.array([centers[i] for i in needed], dtype=np.float32)

    ref_w = template["ref_w_mm"]
    ref_h = template["ref_h_mm"]
    Wc = params.canonical_w
    Hc = int(round(Wc * ref_h / ref_w))
    dst = np.array([[0, 0], [Wc, 0], [Wc, Hc], [0, Hc]], dtype=np.float32)

    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(gray, M, (Wc, Hc))
    return warped, (Wc, Hc)


def _binarize(warped: np.ndarray) -> np.ndarray:
    """마킹(어두움)=255 인 이진 이미지 반환."""
    blur = cv2.GaussianBlur(warped, (5, 5), 0)
    # 조명 불균일에 강한 적응형 임계 + 반전(어두운 마킹 → 흰색)
    binv = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 12
    )
    return binv


def _fill_ratio(binv: np.ndarray, cx: int, cy: int, r: int) -> float:
    h, w = binv.shape
    x0, x1 = max(0, cx - r), min(w, cx + r + 1)
    y0, y1 = max(0, cy - r), min(h, cy + r + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    patch = binv[y0:y1, x0:x1]
    mask = np.zeros(patch.shape, np.uint8)
    cv2.circle(mask, (cx - x0, cy - y0), r, 255, -1)
    area = int((mask > 0).sum())
    if area == 0:
        return 0.0
    return float(((patch > 0) & (mask > 0)).sum()) / area


def _judge_group(fills: dict, params: ReadParams):
    """채움률 딕셔너리로 선택/상태 판정."""
    ordered = sorted(fills.items(), key=lambda kv: kv[1], reverse=True)
    best_v, best_f = ordered[0]
    second_f = ordered[1][1] if len(ordered) > 1 else 0.0
    if best_f < params.mark_abs_min:
        return None, "blank"
    if second_f >= params.mark_abs_min and (best_f == 0 or second_f / best_f >= params.ambiguous_ratio):
        return best_v, "multiple"
    return best_v, "ok"


def read_omr(image_path: str, template_path: str, params: ReadParams | None = None,
             debug_out: str | None = None) -> ReadResult:
    params = params or ReadParams()
    with open(template_path, encoding="utf-8") as fp:
        template = json.load(fp)

    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"이미지를 열 수 없습니다: {image_path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # QR (원본에서)
    exam_id = None
    student_id_qr = None
    try:
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(gray)
        if data and "|" in data:
            exam_id, sid = data.split("|", 1)
            student_id_qr = sid or None
        elif data:
            exam_id = data
    except Exception:
        pass

    centers = _detect_markers(gray)
    warped, (Wc, Hc) = _warp_to_canonical(gray, centers, template, params)
    binv = _binarize(warped)

    ref_w = template["ref_w_mm"]
    r = max(3, int(round(template["bubble_radius_mm"] / ref_w * Wc * 0.85)))

    # 버블별 채움률
    readings: list[BubbleReading] = []
    for b in template["bubbles"]:
        cx = int(round(b["u"] * Wc))
        cy = int(round(b["v"] * Hc))
        f = _fill_ratio(binv, cx, cy, r)
        readings.append(BubbleReading(b["role"], b["index"], b["value"], f))

    # 그룹핑
    q_groups: dict[int, dict] = {}
    id_groups: dict[int, dict] = {}
    for rd in readings:
        tgt = q_groups if rd.role == "question" else id_groups
        tgt.setdefault(rd.index, {})[rd.value] = rd.fill

    questions: dict[int, GroupResult] = {}
    id_columns: dict[int, GroupResult] = {}
    review_flags: list = []

    for q in sorted(q_groups):
        chosen, status = _judge_group(q_groups[q], params)
        questions[q] = GroupResult(q, chosen, status, q_groups[q])
        if status != "ok":
            review_flags.append({"type": "question", "no": q, "status": status})

    id_digits = ""
    for col in sorted(id_groups):
        chosen, status = _judge_group(id_groups[col], params)
        id_columns[col] = GroupResult(col, chosen, status, id_groups[col])
        id_digits += str(chosen) if (status == "ok" and chosen is not None) else "?"
        if status != "ok":
            review_flags.append({"type": "id", "col": col, "status": status})
    student_id_bubbles = id_digits if id_digits and "?" not in id_digits else (id_digits or None)

    if debug_out:
        _draw_debug(warped, template, questions, id_columns, r, Wc, Hc, debug_out)

    return ReadResult(
        exam_id=exam_id or template.get("exam_id"),
        student_id_qr=student_id_qr,
        student_id_bubbles=student_id_bubbles,
        questions=questions,
        id_columns=id_columns,
        review_flags=review_flags,
        warped_shape=(Wc, Hc),
    )


def _draw_debug(warped, template, questions, id_columns, r, Wc, Hc, out_path):
    vis = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
    color = {"ok": (0, 170, 0), "blank": (160, 160, 160), "multiple": (0, 0, 220)}
    lut = {(b["role"], b["index"], b["value"]): b for b in template["bubbles"]}
    for grp, role in ((questions, "question"), (id_columns, "id")):
        for key, g in grp.items():
            for value, fill in g.fills.items():
                b = lut[(role, key, value)]
                cx, cy = int(b["u"] * Wc), int(b["v"] * Hc)
                picked = (value == g.chosen)
                c = color.get(g.status, (200, 200, 0)) if picked else (210, 210, 210)
                cv2.circle(vis, (cx, cy), r, c, 2 if picked else 1)
    cv2.imwrite(out_path, vis)
