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
    chosen: int | None       # 가장 진하게 칠해진 value (question: 0-base 보기 / id: 숫자) / None
    status: str              # "ok" | "blank" | "multiple"
    fills: dict              # {value: fill_ratio}
    # 실제로 칠해진 것으로 판정한 모든 value(오름차순). '모두 고르기' 문항에서
    # 학생이 여러 개를 칠하면 여기에 전부 담긴다. 무응답이면 빈 리스트.
    selected: list = field(default_factory=list)


@dataclass
class ReadResult:
    exam_id: str | None
    # 답안지 QR에 새겨진 레이아웃 지문(구버전 답안지는 None)
    layout_fingerprint: str | None
    student_id_qr: str | None
    student_id_bubbles: str | None
    questions: dict          # {q_no: GroupResult}
    id_columns: dict         # {col: GroupResult}
    review_flags: list       # 사람 검수가 필요한 항목 목록
    warped_shape: tuple

    def answers(self) -> dict:
        """{문항: 1-base 보기번호 또는 None} — 단일 선택 문항 기준."""
        out = {}
        for q, g in self.questions.items():
            out[q] = (g.chosen + 1) if (g.status == "ok" and g.chosen is not None) else None
        return out

    def selections(self) -> dict:
        """{문항: [1-base 보기번호, ...]} — 칠해진 것을 전부 담는다.

        '모두 고르기' 문항은 이 값으로 채점한다. 단일 선택 문항이라면 원소가
        1개(정상) 또는 2개 이상(중복 표기 — 검수 대상)이 된다.
        """
        return {q: [v + 1 for v in g.selected] for q, g in self.questions.items()}

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
    """채움률 딕셔너리로 (최다 선택, 상태, 선택 목록)을 판정한다.

    선택 목록에는 1순위와 '1순위에 견줄 만큼 진한' 나머지를 담는다. 옅은 지움
    자국은 임계를 넘더라도 1순위 대비 비율이 낮으면 제외해, 단일 선택 문항의
    오검출을 막으면서 '모두 고르기'의 복수 표기는 놓치지 않는다.
    """
    ordered = sorted(fills.items(), key=lambda kv: kv[1], reverse=True)
    best_v, best_f = ordered[0]
    if best_f < params.mark_abs_min:
        return None, "blank", []
    selected = [best_v]
    for value, fill in ordered[1:]:
        if fill >= params.mark_abs_min and fill / best_f >= params.ambiguous_ratio:
            selected.append(value)
    selected.sort()
    if len(selected) > 1:
        return best_v, "multiple", selected
    return best_v, "ok", selected


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
    qr_fingerprint = None
    try:
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(gray)
        if data and "|" in data:
            # "시험ID|수험번호" 또는 "시험ID|수험번호|레이아웃지문"(신형)
            parts = data.split("|")
            exam_id = parts[0] or None
            student_id_qr = (parts[1] if len(parts) > 1 else "") or None
            qr_fingerprint = (parts[2] if len(parts) > 2 else "") or None
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
        chosen, status, selected = _judge_group(q_groups[q], params)
        questions[q] = GroupResult(q, chosen, status, q_groups[q], selected)
        if status != "ok":
            # 복수 표기는 '모두 고르기' 문항이면 정상이므로, 무엇이 칠해졌는지 함께 넘긴다.
            review_flags.append({
                "type": "question", "no": q, "status": status,
                "selected": [v + 1 for v in selected],
            })

    # 자리별 판독 → 왼쪽부터 채워쓰기 규약(뒤쪽 빈 칸은 미기입으로 간주해 절삭)
    id_cells = []            # (col, digit or None, blank?)
    for col in sorted(id_groups):
        chosen, status, selected = _judge_group(id_groups[col], params)
        id_columns[col] = GroupResult(col, chosen, status, id_groups[col], selected)
        digit = str(chosen) if (status == "ok" and chosen is not None) else None
        id_cells.append((col, digit, status == "blank"))
    # 뒤쪽 연속 빈칸 제거(4~5자리 좌측정렬 대응)
    while id_cells and id_cells[-1][2]:
        id_cells.pop()
    id_digits = ""
    for col, digit, _blank in id_cells:
        id_digits += digit if digit is not None else "?"
        if digit is None:  # 중간의 빈칸/중복은 검수 대상
            review_flags.append({"type": "id", "col": col, "status": "review"})
    student_id_bubbles = id_digits or None

    if debug_out:
        _draw_debug(warped, template, questions, id_columns, r, Wc, Hc, debug_out)

    return ReadResult(
        exam_id=exam_id or template.get("exam_id"),
        layout_fingerprint=qr_fingerprint,
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
                picked = value in g.selected
                c = color.get(g.status, (200, 200, 0)) if picked else (210, 210, 210)
                cv2.circle(vis, (cx, cy), r, c, 2 if picked else 1)
    cv2.imwrite(out_path, vis)
