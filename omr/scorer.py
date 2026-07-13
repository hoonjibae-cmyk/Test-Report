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

    @classmethod
    def load(cls, path: str) -> "AnswerKey":
        with open(path, encoding="utf-8") as fp:
            d = json.load(fp)
        answers = {int(k): int(v) for k, v in d["answers"].items()}
        default_pt = d.get("default_point", 1.0)
        points = {q: float(d.get("points", {}).get(str(q), default_pt)) for q in answers}
        return cls(d.get("exam_id", "EXAM"), d.get("title", ""), answers, points)

    @property
    def total_points(self) -> float:
        return sum(self.points.values())


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
