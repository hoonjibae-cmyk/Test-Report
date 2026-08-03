"""채점 및 통계.

판독 결과(answers)를 정답키와 대조해 원점수를 산출하고,
응시자 집합에 대한 기초 통계(평균·표준편차·석차·백분위)를 계산한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class AnswerKey:
    exam_id: str
    title: str
    answers: dict          # {문항no(int): 정답 보기 1-base(int)}
    points: dict           # {문항no(int): 배점(float)}
    subject: str = ""      # "english" 등 (성적표 유형 선택에 사용)
    grade_cuts: list = field(default_factory=list)  # 절대평가 등급컷 [90,80,...]
    qmeta: dict = field(default_factory=dict)        # {문항no: {"area","type","difficulty"}}

    @classmethod
    def load(cls, path: str) -> "AnswerKey":
        with open(path, encoding="utf-8") as fp:
            d = json.load(fp)
        answers = {int(k): int(v) for k, v in d["answers"].items()}
        default_pt = d.get("default_point", 1.0)
        points = {q: float(d.get("points", {}).get(str(q), default_pt)) for q in answers}
        qmeta = {int(k): v for k, v in d.get("question_meta", {}).items()}
        return cls(
            exam_id=d.get("exam_id", "EXAM"),
            title=d.get("title", ""),
            answers=answers,
            points=points,
            subject=d.get("subject", ""),
            grade_cuts=list(d.get("grade_cuts", [])),
            qmeta=qmeta,
        )

    @property
    def total_points(self) -> float:
        return sum(self.points.values())


def compute_grade(raw: float, cuts: list) -> int | None:
    """절대평가 등급컷으로 등급 산출. cuts=[90,80,...] → 90↑=1등급.

    cuts가 없으면 None. 마지막 컷 미만이면 (len(cuts)+1)등급.
    """
    if not cuts:
        return None
    for i, cut in enumerate(cuts):
        if raw >= cut:
            return i + 1
    return len(cuts) + 1


# 영어 독해 세부 유형 → 대분류 매핑(정답키에 category가 없을 때 자동 적용).
# 표준 수능·모의고사 유형 분류를 따른다.
CATEGORY_MAP = {
    # 대의 파악
    "목적": "대의 파악", "심경·분위기": "대의 파악", "심경": "대의 파악",
    "주장": "대의 파악", "함의 추론": "대의 파악", "요지": "대의 파악",
    "주제": "대의 파악", "제목": "대의 파악",
    # 세부 내용
    "내용 일치": "세부 내용", "도표 일치": "세부 내용", "도표": "세부 내용",
    "실용문": "세부 내용", "지칭 추론": "세부 내용",
    # 어법·어휘
    "어법": "어법·어휘", "어휘": "어법·어휘",
    # 빈칸 추론
    "빈칸 추론": "빈칸 추론",
    # 간접 쓰기
    "무관한 문장": "간접 쓰기", "글의 순서": "간접 쓰기",
    "문장 삽입": "간접 쓰기", "문단 요약": "간접 쓰기",
    # 장문 독해
    "장문 독해": "장문 독해",
}
# 대분류 표시 순서(성취율과 무관하게 교육과정 흐름 순)
CATEGORY_ORDER = ["대의 파악", "세부 내용", "어법·어휘", "빈칸 추론", "간접 쓰기", "장문 독해"]


def resolve_reading_category(key: AnswerKey, q: int) -> str | None:
    """문항 q의 독해 대분류. 듣기 문항은 제외(None)."""
    m = key.qmeta.get(q, {})
    if m.get("area") == "듣기":
        return None
    return m.get("category") or CATEGORY_MAP.get(m.get("type")) or m.get("type")


def _group_stats(answers: dict, key: AnswerKey, keyfunc, order=None) -> list[dict]:
    """keyfunc(q)로 문항을 묶어 성취율 집계. order 지정 시 그 순서, 아니면 성취율 내림차순."""
    groups: dict = {}
    for q in key.answers:
        name = keyfunc(q)
        if not name:
            continue
        g = groups.setdefault(name, {"name": name, "earned": 0.0, "possible": 0.0,
                                     "correct": 0, "count": 0})
        pt = key.points[q]
        g["possible"] += pt
        g["count"] += 1
        if answers.get(q) == key.answers[q]:
            g["earned"] += pt
            g["correct"] += 1
    out = []
    for g in groups.values():
        g["rate"] = round(g["earned"] / g["possible"] * 100, 1) if g["possible"] else 0.0
        g["earned"] = round(g["earned"], 3)
        g["possible"] = round(g["possible"], 3)
        out.append(g)
    if order:
        idx = {n: i for i, n in enumerate(order)}
        out.sort(key=lambda x: idx.get(x["name"], len(order)))
    else:
        out.sort(key=lambda x: x["rate"], reverse=True)
    return out


def category_stats(answers: dict, key: AnswerKey, field_name: str) -> list[dict]:
    """문항 메타의 특정 필드(area/type/difficulty)별 성취율 집계(성취율 내림차순)."""
    return _group_stats(answers, key, lambda q: key.qmeta.get(q, {}).get(field_name))


def reading_category_stats(answers: dict, key: AnswerKey) -> list[dict]:
    """독해 대분류별 성취율(교육과정 순)."""
    return _group_stats(answers, key, lambda q: resolve_reading_category(key, q),
                        order=CATEGORY_ORDER)


def _cohort_group(records: list, key: AnswerKey, keyfunc) -> dict:
    groups: dict = {}
    for rec in records:
        ans = rec.get("answers", {})
        for q in key.answers:
            name = keyfunc(q)
            if not name:
                continue
            g = groups.setdefault(name, {"earned": 0.0, "possible": 0.0})
            g["possible"] += key.points[q]
            if ans.get(q) == key.answers[q]:
                g["earned"] += key.points[q]
    return {k: (round(v["earned"] / v["possible"] * 100, 1) if v["possible"] else 0.0)
            for k, v in groups.items()}


def cohort_category_stats(records: list, key: AnswerKey, field_name: str) -> dict:
    """응시 집단의 필드별 평균 성취율. {유형명: rate(%)}."""
    return _cohort_group(records, key, lambda q: key.qmeta.get(q, {}).get(field_name))


def cohort_analysis(records: list, key: AnswerKey) -> dict:
    """집단 평균(영역·대분류·난이도별)을 한 번에 계산."""
    return {
        "area": _cohort_group(records, key, lambda q: key.qmeta.get(q, {}).get("area")),
        "category": _cohort_group(records, key, lambda q: resolve_reading_category(key, q)),
        "difficulty": _cohort_group(records, key, lambda q: key.qmeta.get(q, {}).get("difficulty")),
    }


def english_analysis(answers: dict, key: AnswerKey) -> dict:
    """영어 모의고사 심화 분석: 등급 + 영역별/독해 대분류별/난이도별 성취율."""
    detail = score_one(answers, key)
    return {
        "grade": compute_grade(detail["raw_score"], key.grade_cuts),
        "grade_cuts": key.grade_cuts,
        "area_stats": category_stats(answers, key, "area"),
        "category_stats": reading_category_stats(answers, key),  # 독해 대분류
        "type_stats": category_stats(answers, key, "type"),      # 세부 유형(툴팁/상세용)
        "difficulty_stats": category_stats(answers, key, "difficulty"),
    }


def score_one(answers: dict, key: AnswerKey) -> dict:
    """단일 응시자 채점. answers: {문항: 1-base 또는 None}."""
    correct, wrong, blank = [], [], []
    raw = 0.0
    for q, ans in key.answers.items():
        got = answers.get(q)
        if got is None:
            blank.append(q)
        elif got == ans:
            correct.append(q)
            raw += key.points[q]
        else:
            wrong.append(q)
    return {
        "raw_score": round(raw, 3),
        "total_points": key.total_points,
        "num_correct": len(correct),
        "num_wrong": len(wrong),
        "num_blank": len(blank),
        "correct": correct,
        "wrong": wrong,
        "blank": blank,
    }


def score_batch(records: list[dict], key: AnswerKey) -> list[dict]:
    """records: [{"student_id","name"?,"answers"}]. 통계 포함 결과 반환."""
    scored = []
    for rec in records:
        s = score_one(rec["answers"], key)
        scored.append({
            "student_id": rec.get("student_id"),
            "name": rec.get("name", ""),
            **s,
        })

    raws = [s["raw_score"] for s in scored]
    n = len(raws)
    mean = sum(raws) / n if n else 0.0
    var = sum((x - mean) ** 2 for x in raws) / n if n else 0.0
    std = var ** 0.5

    # 석차(동점 공동석차) 및 백분위
    order = sorted(raws, reverse=True)
    for s in scored:
        rank = order.index(s["raw_score"]) + 1
        below = sum(1 for x in raws if x < s["raw_score"])
        s["rank"] = rank
        s["percentile"] = round(below / n * 100, 1) if n else 0.0
        s["class_mean"] = round(mean, 2)
        s["class_std"] = round(std, 2)
        s["class_size"] = n
    return scored
