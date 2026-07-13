"""OMR 시험 채점 시스템 (MVP: OMR 생성 + 판독).

모듈 구성:
    layout    OMR 시트의 기하 배치를 mm 단위로 계산 (생성/판독 공용)
    generator OMR PDF 및 미리보기 이미지 생성, 판독용 템플릿 JSON 저장
    reader    스캔 이미지를 ArUco 정렬 마커 기준으로 보정 후 마킹 판독
    scorer    판독 결과를 정답키와 대조하여 채점 및 통계 산출
    simulate  물리 스캐너 없이 파이프라인을 검증하기 위한 가상 마킹 유틸
"""

__version__ = "0.1.0"
