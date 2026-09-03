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
import statistics
from dataclasses import dataclass, field

import cv2
import numpy as np


# 판정 파라미터 (기본값은 스캐너 입력 기준 보수적으로 설정)
@dataclass
class ReadParams:
    canonical_w: int = 1400          # 정준 좌표계 가로 px
    mark_abs_min: float = 0.30       # 마킹으로 인정할 최소 채움률
    ambiguous_ratio: float = 0.70    # 2순위/1순위 비율이 이 값 이상이면 중복 의심

    # --- 판정 여유(margin) ---
    # 위 두 기준은 '어떻게 읽을지'만 정할 뿐, '얼마나 확실한지'는 말해 주지 않는다.
    # 채움률 0.02(완전 빈칸)와 0.29(흐린 표기)는 둘 다 미표기로 읽히지만 확신은
    # 전혀 다르다. 아래 값으로 경계에서 얼마나 떨어져야 '확실'로 볼지 정한다.
    # 확실한 것만 자동 검수 통과시키고, 경계 근처(회색지대)는 사람에게 넘긴다.
    # 기준값은 왜곡·노이즈를 넣은 모의 스캔에서 실측해 잡았다. 빈 버블도 인쇄된
    # 숫자와 종이 질감 때문에 채움률이 0이 아니라 최대 0.19까지 올라가고, 실제
    # 마킹은 0.86 언저리에 몰린다. 그래서 '확실한 빈칸' 선을 노이즈 바닥보다
    # 위(0.30×0.85≈0.26)에 두어, 노이즈를 애매한 표기로 오해하지 않게 한다.
    blank_margin: float = 0.85       # 1순위 < mark_abs_min×이 값 → 확실한 미표기
    blank_spread: float = 1.8        # 1순위 ≤ 무리 중앙값×이 값 → 튀는 것이 없으니 미표기
    mark_margin: float = 1.35        # 1순위 ≥ mark_abs_min×이 값 → 확실한 표기
    ratio_margin: float = 0.75       # 2순위/1순위 ≤ ambiguous_ratio×이 값 → 확실한 단독


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
    # 판정이 경계에서 충분히 떨어져 있는가. False면 사람이 눈으로 봐야 한다.
    certain: bool = True


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
    # 검수 화면에 띄울 미리보기 — 보정된 이미지에 판독 결과를 그린 JPEG.
    # 필요할 때만 만든다(make_preview=True).
    preview_jpeg: bytes | None = None
    # {문항번호: JPEG} — 주관식 손기입 칸을 반듯하게 편 이미지(전사용)
    essay_crops: dict = field(default_factory=dict)

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

    def id_conflict(self) -> bool:
        """QR과 버블이 서로 다른 수험번호를 가리키는가.

        학생별 답안지로 출력했을 때만 의미가 있다(빈 답안지는 QR에 수험번호가
        없다). 둘이 어긋났다는 건 답안지를 다른 학생이 썼거나 마킹을 잘못한
        것이므로, 성적표가 엉뚱한 학생에게 가지 않도록 반드시 사람이 봐야 한다.
        """
        if not self.student_id_qr or not self.student_id_bubbles:
            return False
        return self.student_id_qr.lstrip("0") != self.student_id_bubbles.lstrip("0")

    def uncertain_questions(self) -> list:
        """판정이 경계에 걸쳐 사람이 눈으로 봐야 하는 문항 번호."""
        return sorted(q for q, g in self.questions.items() if not g.certain)

    def multi_marked_questions(self) -> list:
        """둘 이상 칠해진 것으로 읽은 문항 번호.

        '모두 고르기' 문항이면 정상이라 판독기는 판단하지 않는다. 어떤 문항이
        복수 정답인지 아는 채점 쪽에서 정상/검수를 가른다.
        """
        return sorted(q for q, g in self.questions.items() if len(g.selected) > 1)


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
    return warped, (Wc, Hc), M


def _crop_essay_boxes(gray: np.ndarray, template: dict, M: np.ndarray,
                      Wc: int, Hc: int, scale: float = 3.0,
                      quality: int = 88) -> dict:
    """주관식 손기입 칸을 반듯하게 펴서 잘라낸다.

    정준 이미지(가로 1400px)에서 자르면 한 칸이 400px 남짓이라 손글씨 획이
    뭉개진다. 그래서 원본 스캔에서 그 자리만 다시 펴서 배로 키워 잘라낸다.
    글자를 읽어야 하는 이미지이므로 해상도가 정확도를 좌우한다.

    반환: {문항번호: JPEG bytes}
    """
    boxes = template.get("essay_boxes") or []
    if not boxes:
        return {}
    try:
        inv = np.linalg.inv(M)
    except np.linalg.LinAlgError:
        return {}

    out = {}
    for box in boxes:
        # 정준 좌표의 네 모서리를 원본 스캔 좌표로 되돌린다
        corners = np.array([
            [box["u0"] * Wc, box["v0"] * Hc],
            [box["u1"] * Wc, box["v0"] * Hc],
            [box["u1"] * Wc, box["v1"] * Hc],
            [box["u0"] * Wc, box["v1"] * Hc],
        ], dtype=np.float32).reshape(-1, 1, 2)
        src = cv2.perspectiveTransform(corners, inv).reshape(4, 2).astype(np.float32)

        w = max(1, int(round((box["u1"] - box["u0"]) * Wc * scale)))
        h = max(1, int(round((box["v1"] - box["v0"]) * Hc * scale)))
        dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
        patch = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(src, dst), (w, h))

        ok, buf = cv2.imencode(".jpg", patch, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            out[int(box["num"])] = buf.tobytes()
    return out


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
    """채움률 딕셔너리로 (최다 선택, 상태, 선택 목록, 확신 여부)를 판정한다.

    선택 목록에는 1순위와 '1순위에 견줄 만큼 진한' 나머지를 담는다. 옅은 지움
    자국은 임계를 넘더라도 1순위 대비 비율이 낮으면 제외해, 단일 선택 문항의
    오검출을 막으면서 '모두 고르기'의 복수 표기는 놓치지 않는다.

    확신 여부는 판정 자체와 별개다. 같은 '미표기'라도 아무것도 안 칠한 답안지와
    연필이 흐려 임계를 아슬아슬하게 못 넘긴 답안지를 구분해야, 사람이 봐야 할
    것만 골라낼 수 있다.
    """
    ordered = sorted(fills.items(), key=lambda kv: kv[1], reverse=True)
    best_v, best_f = ordered[0]
    second_f = ordered[1][1] if len(ordered) > 1 else 0.0

    if best_f < params.mark_abs_min:
        # 미표기다. 남은 문제는 '확실한가' — 아무것도 안 칠한 것과, 연필이 흐려
        # 임계를 못 넘긴 것을 갈라야 사람이 볼 것만 골라낼 수 있다.
        #
        # 절대값만으로는 갈리지 않는다. 종이 질감·스캐너 노출·버블 안에 인쇄된
        # 숫자 때문에 빈 칸의 채움률도 0.25 언저리까지 올라가는 스캔이 있고,
        # 그러면 멀쩡한 빈 칸이 물음표가 된다.
        #
        # 흐린 자국은 **무리 안에서 튄다.** 실측하면 이렇게 갈린다.
        #   완전히 빈 자리      최대÷중앙  1.1~1.4배
        #   가장 흐린 연필 자국 최대÷중앙  2.3배 이상
        # 그래서 절대값이 낮거나, 낮지 않아도 무리 안에서 튀지 않으면 빈칸으로
        # 확신한다. 둘 다 아니면 사람에게 넘긴다.
        typical = statistics.median(fills.values())
        certain = (best_f < params.mark_abs_min * params.blank_margin
                   or best_f <= typical * params.blank_spread)
        return None, "blank", [], certain

    selected = [best_v]
    for value, fill in ordered[1:]:
        if fill >= params.mark_abs_min and fill / best_f >= params.ambiguous_ratio:
            selected.append(value)
    selected.sort()

    # 칠해진 것으로 본 모든 보기가 임계에서 충분히 위에 있어야 확실하다.
    strong = all(fills[v] >= params.mark_abs_min * params.mark_margin for v in selected)

    if len(selected) > 1:
        # 중복 표기 자체는 '무엇이 칠해졌나'만 말한다. 단일 선택 문항이라면
        # 어느 것을 인정할지 사람이 정해야 하므로, 판단은 호출 측에 맡긴다.
        return best_v, "multiple", selected, strong

    # 단독 표기는 2순위가 중복 의심선에서도 충분히 아래여야 확실하다.
    clear = second_f / best_f <= params.ambiguous_ratio * params.ratio_margin
    return best_v, "ok", selected, strong and clear


def read_omr(image_path: str, template_path: str, params: ReadParams | None = None,
             debug_out: str | None = None, make_preview: bool = False,
             preview_width: int = 1100, preview_quality: int = 72,
             make_essay_crops: bool = False) -> ReadResult:
    """스캔 한 장을 판독한다.

    make_preview=True면 검수 화면용 미리보기(JPEG)를 함께 만든다. 원본이 아니라
    **판독기가 실제로 본 이미지**(원근 보정 후 + 감지 결과 표시)라, 잘못 읽힌 경우
    사람이 이유를 바로 알 수 있다.
    """
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
    warped, (Wc, Hc), M = _warp_to_canonical(gray, centers, template, params)
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
        chosen, status, selected, certain = _judge_group(q_groups[q], params)
        questions[q] = GroupResult(q, chosen, status, q_groups[q], selected, certain)
        if status != "ok":
            # 복수 표기는 '모두 고르기' 문항이면 정상이므로, 무엇이 칠해졌는지 함께 넘긴다.
            review_flags.append({
                "type": "question", "no": q, "status": status,
                "selected": [v + 1 for v in selected],
                # 확실한 미표기(=학생이 그냥 안 푼 문항)는 사람이 볼 필요가 없다.
                "certain": certain,
            })

    # 자리별 판독 → 왼쪽부터 채워쓰기 규약(뒤쪽 빈 칸은 미기입으로 간주해 절삭)
    id_cells = []            # (col, digit or None, blank?)
    for col in sorted(id_groups):
        chosen, status, selected, certain = _judge_group(id_groups[col], params)
        id_columns[col] = GroupResult(col, chosen, status, id_groups[col], selected, certain)
        digit = str(chosen) if (status == "ok" and chosen is not None) else None
        id_cells.append((col, digit, status == "blank", certain))
    # 뒤쪽 연속 빈칸 제거(4~5자리 좌측정렬 대응) — 단, 확실한 빈칸만 잘라낸다.
    # 흐린 표기를 빈칸으로 오해해 잘라 버리면 수험번호가 조용히 짧아진다.
    while id_cells and id_cells[-1][2] and id_cells[-1][3]:
        id_cells.pop()
    id_digits = ""
    for col, digit, _blank, certain in id_cells:
        id_digits += digit if digit is not None else "?"
        if digit is None:  # 중간의 빈칸/중복은 검수 대상
            review_flags.append({"type": "id", "col": col, "status": "review", "certain": False})
        elif not certain:  # 읽기는 했지만 경계에 걸친 자리 — 수험번호는 틀리면 치명적이라 확인
            review_flags.append({"type": "id", "col": col, "status": "weak", "certain": False})
    student_id_bubbles = id_digits or None

    # 주관식 칸 이미지 — 전사(글자 읽기)에 쓴다. 필요할 때만 만든다.
    essay_crops = _crop_essay_boxes(gray, template, M, Wc, Hc) if make_essay_crops else {}

    preview_jpeg = None
    if debug_out or make_preview:
        vis = _annotate(warped, template, questions, id_columns, r, Wc, Hc)
        if debug_out:
            cv2.imwrite(debug_out, vis)
        if make_preview:
            preview_jpeg = _encode_preview(vis, preview_width, preview_quality)

    return ReadResult(
        exam_id=exam_id or template.get("exam_id"),
        layout_fingerprint=qr_fingerprint,
        student_id_qr=student_id_qr,
        student_id_bubbles=student_id_bubbles,
        questions=questions,
        id_columns=id_columns,
        review_flags=review_flags,
        warped_shape=(Wc, Hc),
        preview_jpeg=preview_jpeg,
        essay_crops=essay_crops,
    )


def _encode_preview(vis, width: int, quality: int) -> bytes | None:
    """검수 화면용으로 폭을 줄여 JPEG로 인코딩한다(전송량 절감)."""
    h, w = vis.shape[:2]
    if w > width:
        scale = width / w
        vis = cv2.resize(vis, (width, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


def _annotate(warped, template, questions, id_columns, r, Wc, Hc):
    """보정된 이미지 위에 버블별 판정 결과를 그린 컬러 이미지를 만든다.

    확신 없는 판정(회색지대)은 주황으로 따로 표시한다. 검수 화면에 걸린 답안지가
    '왜 걸렸는지'를 사람이 그림에서 바로 찾을 수 있어야 하기 때문이다.
    """
    vis = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
    color = {"ok": (0, 170, 0), "blank": (160, 160, 160), "multiple": (0, 0, 220)}
    uncertain_c = (0, 150, 255)      # 주황(BGR) — 경계에 걸쳐 사람이 봐야 하는 자리
    lut = {(b["role"], b["index"], b["value"]): b for b in template["bubbles"]}
    for grp, role in ((questions, "question"), (id_columns, "id")):
        for key, g in grp.items():
            best = max(g.fills.values()) if g.fills else 0.0
            for value, fill in g.fills.items():
                b = lut[(role, key, value)]
                cx, cy = int(b["u"] * Wc), int(b["v"] * Hc)
                picked = value in g.selected
                if picked:
                    c = uncertain_c if not g.certain else color.get(g.status, (200, 200, 0))
                elif not g.certain and fill >= best:
                    # 표기로 인정하진 않았지만 이 자리 때문에 애매해진 것 — 짚어 준다
                    c = uncertain_c
                    picked = True
                else:
                    c = (210, 210, 210)
                cv2.circle(vis, (cx, cy), r, c, 2 if picked else 1)
    return vis
