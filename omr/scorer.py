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


def category_stats(answers: dict, key: AnswerKey, field_name: str) -> list[dict]:
    """문항 메타의 특정 필드(area/type/difficulty)별 성취율 집계.

    반환: [{"name","earned","possible","correct","count","rate"}] (성취율 내림차순).
    """
    groups: dict = {}
    for q in key.answers:
        meta = key.qmeta.get(q, {})
        name = meta.get(field_name)
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
    out.sort(key=lambda x: x["rate"], reverse=True)
    return out


def cohort_category_stats(records: list, key: AnswerKey, field_name: str) -> dict:
    """응시 집단 전체의 필드별 평균 성취율. {유형명: rate(%)}.

    rate = (집단 정답 배점 합) / (집단 배점 합) × 100.
    """
    groups: dict = {}
    for rec in records:
        ans = rec.get("answers", {})
        for q in key.answers:
            name = key.qmeta.get(q, {}).get(field_name)
            if not name:
                continue
            g = groups.setdefault(name, {"earned": 0.0, "possible": 0.0})
            g["possible"] += key.points[q]
            if ans.get(q) == key.answers[q]:
                g["earned"] += key.points[q]
    return {k: (round(v["earned"] / v["possible"] * 100, 1) if v["possible"] else 0.0)
            for k, v in groups.items()}


def cohort_analysis(records: list, key: AnswerKey) -> dict:
    """집단 평균(영역·유형·난이도별)을 한 번에 계산."""
    return {
        "area": cohort_category_stats(records, key, "area"),
        "type": cohort_category_stats(records, key, "type"),
        "difficulty": cohort_category_stats(records, key, "difficulty"),
    }


def english_analysis(answers: dict, key: AnswerKey) -> dict:
    """영어 모의고사 심화 분석: 등급 + 영역별/유형별/난이도별 성취율."""
    detail = score_one(answers, key)
    return {
        "grade": compute_grade(detail["raw_score"], key.grade_cuts),
        "grade_cuts": key.grade_cuts,
        "area_stats": category_stats(answers, key, "area"),
        "type_stats": category_stats(answers, key, "type"),
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
