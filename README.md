# OMR 시험 채점 시스템

OMR(광학 마크 인식) 답안지를 **직접 생성**하고, 마킹된 **스캔본을 자동 판독**하여
**채점·통계**까지 처리하는 시스템입니다. 현재 1차 MVP 범위(생성 + 판독 + 채점)가
구현·검증되어 있습니다.

> 검토 결론: OMR 생성·판독을 포함한 전체 기능은 구현 가능하며, 본 저장소가 그 핵심
> 파이프라인(생성→마킹→스캔→판독→채점)을 실제로 동작하는 코드로 증명합니다.
> 무(無)스캐너 자체검증에서 왜곡을 준 스캔본 25장(문항 1,000개)에 대해 **문항 인식
> 100%, 학번 인식 100%**를 달성했습니다.

## 왜 정확한가 — 설계 원리

"내가 생성한 OMR을 내가 판독"하므로 정렬 기준을 완전히 통제합니다.

- **ArUco 코너 마커 4종**: 코너마다 고유 ID를 부여해 방향·위치를 명확히 판별.
  스캔이 기울거나 크기가 달라도 원근 변환(homography)으로 정준 좌표계로 보정.
- **정규화 좌표 템플릿**: 생성 시 만든 버블 좌표(JSON)를 판독기가 그대로 사용 →
  좌표 어긋남 없음.
- **QR 식별**: 시험코드·응시자ID를 시트에 삽입해 어떤 시험/학생인지 자동 확정.
- **검수 안전장치**: 이중 마킹·무응답·애매한 마킹은 임의 판정하지 않고
  `review` 플래그로 분리 → 오채점 방지.

## 설치

```bash
pip install -r requirements.txt
# 한글 답안지 렌더링을 위해 한글 폰트 필요(예: 나눔고딕)
#   Debian/Ubuntu: apt-get install fonts-nanum
#   또는 환경변수로 지정:  export OMR_FONT=/path/to/한글폰트.ttf
```

## 사용법 (CLI)

```bash
# 1) 답안지(PDF)·판독 템플릿(JSON)·미리보기(PNG) 생성
python -m omr.cli generate --exam MID2026 --title "1학기 중간고사" \
    --questions 40 --choices 5 --id-digits 8 --out output

# 2) 스캔 이미지 판독 (결과 JSON 출력, --debug로 판독 오버레이 저장)
python -m omr.cli read  --image scan.png \
    --template output/MID2026_template.json --debug output/dbg.png

# 3) 판독 + 채점 (정답키 대조)
python -m omr.cli score --image scan.png \
    --template output/MID2026_template.json --key examples/answer_key.json

# 4) 파이프라인 자체검증 (스캐너 없이 전체 흐름 확인)
python -m omr.cli selftest --out output

# 5) 스캔 폴더 일괄 처리 → 채점 + 웹링크 성적표 일괄 생성
python -m omr.cli batch \
    --scans scans/ --template output/MID2026_template.json \
    --key examples/answer_key.json \
    --exam MID2026 --title "1학기 중간고사" --date "2026-04-28" \
    --school "한빛중학교 3-2" --roster examples/students.json \
    --base-url "https://reports.school.kr/mid2026" \
    --salt "여기에-비공개-고정값" --out output

# 6) 생성된 성적표 로컬 미리보기
python -m omr.cli serve --dir output/reports/MID2026 --port 8000
```

## 웹링크 형식 성적표

성적표는 **응시자별 자체완결형(single-file) HTML**로 생성됩니다. 각 파일명은
추측 불가능한 토큰(`{20자리 hex}.html`)이라, 정적 호스팅(S3·Netlify·GitHub Pages·
학교 서버 등) 어디에 올려도 **링크만으로 안전하게 열람**됩니다. 모바일 우선 디자인이라
학부모가 폰에서 바로 봅니다.

- **링크 안정성**: 토큰은 `(salt, 시험코드, 학번)`으로 결정론적 파생 → 재생성해도
  동일 응시자의 링크가 바뀌지 않습니다(알림톡 발송 후에도 유효). **운영 시 `--salt`를
  반드시 비공개 고정값으로 지정**하세요(토큰 추측 방지).
- **성적표 내용**: 점수·등급·백분위·석차, 반 평균 대비 막대, 문항별 O/X/무응답 표,
  판독 검수 안내(이중표기·무응답 시).
- **`manifest.json`**: `학번·이름·점수·석차·링크(url)` 목록. **다음 단계 알림톡 발송의
  입력**이 됩니다(링크 변수로 사용).
- **`index.html`**(교사용): 전체 응시자 링크 목록. **`results.csv`**: 채점 결과표.

`batch` 대신, 이미 판독·집계한 데이터가 있으면 `report --records records.json` 으로
성적표만 생성할 수 있습니다.

### 응시자별 시트 생성
`--students students.json` 으로 응시자별 QR(사전 배정 학번)을 넣은 시트를 만듭니다.
```json
[{"id": "20250001", "name": "홍길동"}, {"id": "20250002", "name": "김철수"}]
```

### 정답키 형식 (`examples/answer_key.json`)
```json
{
  "exam_id": "MID2026",
  "title": "1학기 중간고사",
  "default_point": 2.5,
  "answers": {"1": 3, "2": 1, "...": "...", "40": 5},
  "points": {"1": 5.0}
}
```
`answers`는 문항→정답 보기(1-base). `points`는 문항별 배점(생략 시 `default_point`).

## 모듈 구조

| 파일 | 역할 |
|------|------|
| `omr/layout.py`    | 시트 기하 배치(mm)를 계산 — 생성기·판독기 공용 |
| `omr/generator.py` | PDF·미리보기 PNG·템플릿 JSON·ArUco·QR 생성 |
| `omr/reader.py`    | 마커 검출→원근보정→이진화→채움률→마킹 판정 |
| `omr/scorer.py`    | 정답 대조 채점 + 평균·표준편차·석차·백분위 |
| `omr/report_web.py`| 웹링크 형식 HTML 성적표 + manifest 생성(토큰 링크) |
| `omr/batch.py`     | 스캔 폴더 일괄 판독→채점→웹성적표 |
| `omr/simulate.py`  | 스캐너 없이 가상 마킹·왜곡으로 스캔본 생성(검증용) |
| `omr/selftest.py`  | 생성→마킹→판독 전체 자체검증 |
| `omr/cli.py`       | `generate`/`read`/`score`/`batch`/`report`/`serve`/`selftest` CLI |

## 판독 파라미터 튜닝
`ReadParams`(또는 `--threshold`)로 조정합니다.
- `mark_abs_min`(기본 0.30): 마킹 인정 최소 채움률. 연필이 흐리면 낮추고, 지저분한
  스캔이면 높입니다.
- `ambiguous_ratio`(기본 0.70): 2순위/1순위 채움률 비율이 이 값 이상이면 이중 마킹
  의심으로 검수 처리.
- `canonical_w`(기본 1400): 정준 좌표계 해상도.

## 테스트
```bash
python tests/test_pipeline.py     # 또는  python -m pytest tests/ -v
```

## 로드맵 (다음 단계)
1. **[완료] 생성 + 판독 + 채점 MVP**
2. **[완료] 웹링크 형식 성적표** — 응시자별 토큰 HTML + manifest + 교사용 목록/CSV
3. **학부모 알림톡** — manifest의 링크를 승인 템플릿 변수에 치환하여 발송대행사
   API로 배치 발송
   *(선행 필요: 카카오 비즈니스 채널 개설, 발송대행사 계약, 템플릿 사전 심사)*

## 운영 팁 (정확도 극대화)
- **평판/ADF 스캐너 200~300dpi 흑백/그레이스케일** 입력을 권장합니다.
- 인쇄 시 "실제 크기(100%, 배율 맞춤 해제)"로 출력해 마커 규격을 유지하세요.
- 스마트폰 촬영도 동작하나, 정면·균일 조명·전체 마커 포함 촬영이 필요합니다.
