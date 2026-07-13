"""폰트 탐색 유틸.

한글 렌더링을 위해 나눔고딕 등 한글 지원 TTF를 찾는다.
환경변수 OMR_FONT 로 명시 지정 가능. 없으면 후보 경로를 순회하고,
최후에는 DejaVuSans(라틴 전용)로 폴백한다. 시스템의 기능 요소(숫자·마커·QR)는
모두 ASCII/그래픽이라 한글 폰트가 없어도 판독에는 영향이 없다.
"""
from __future__ import annotations

import os

_KOREAN_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]
_LATIN_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def find_font() -> str:
    """사용 가능한 폰트 경로를 반환한다."""
    env = os.environ.get("OMR_FONT")
    if env and os.path.exists(env):
        return env
    for p in _KOREAN_CANDIDATES:
        if os.path.exists(p):
            return p
    return _LATIN_FALLBACK
