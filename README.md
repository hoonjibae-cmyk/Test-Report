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
- **디자인 톤앤매너**: 네이비(#183c73)+골드 계열의 학원 리포트 톤. 흰 문서 카드 상단
  네이비 라인, 네이비 학생 정보 스트립, 원점수 강조 셀, 네이비→블루 그라디언트 막대,
  ○/×/– 정오답 히트맵(오답은 강한 빨강). 디자인 토큰·컴포넌트 CSS는 `REPORT_CSS`로
  한 곳에 모여 있어, 앞으로 추가할 여러 성적표 유형이 같은 톤을 공유합니다.
  (폰트는 Pretendard→Noto Sans KR 폴백. 실제 배포 시 Pretendard 웹폰트를 추가하면
  원본 톤과 더 가깝습니다.)
- **성적표 내용**: 점수·성취수준·백분위·석차, 반 평균 대비 막대, 문항별 정오답 히트맵,
  판독 검수 안내(이중표기·무응답 시).
- **`manifest.json`**: `학번·이름·점수·석차·링크(url)` 목록. **다음 단계 알림톡 발송의
  입력**이 됩니다(링크 변수로 사용).
- **`index.html`**(교사용): 전체 응시자 링크 목록. **`results.csv`**: 채점 결과표.

`batch` 대신, 이미 판독·집계한 데이터가 있으면 `report --records records.json` 으로
성적표만 생성할 수 있습니다.

### 성적표 유형 (report type)

`--report-type` 으로 성적표 유형을 고릅니다. 기본값 `auto`는 정답키의 `subject`
값으로 자동 추론합니다. 모든 유형은 동일한 디자인 토큰(`REPORT_CSS`)을 공유합니다.

| 유형 | 값 | 내용 |
|------|----|------|
| 기본형 | `basic` | 점수·성취수준·석차·백분위 + 반평균 비교 + 문항별 정오답 |
| 영어 모의고사 | `english` | 기본형 + **절대평가 등급**, **듣기/독해 영역별 성취**, **독해 유형별 성취율(대분류 6종)**, **우선 보완 유형**, **난이도별 성취율**. 영역·유형·난이도 막대에는 **응시 집단 평균**이 금색 마커로 표시되어 학생 점수와 바로 비교됩니다 |

**독해 유형 대분류**: 세부 유형을 표준 6개 대분류로 묶어 간결하게 보여줍니다 —
대의 파악 / 세부 내용 / 어법·어휘 / 빈칸 추론 / 간접 쓰기 / 장문 독해.
정답키에 `question_meta.category`를 넣으면 그 값을 쓰고, 없으면 세부 `type`을
`CATEGORY_MAP`으로 자동 매핑합니다(듣기는 영역 카드에서 별도 표시).

```bash
python -m omr.cli batch --scans eng_scans/ \
    --template output/ENG2026M03_template.json --key examples/english_answer_key.json \
    --exam ENG2026M03 --title "고1 3월 전국연합 · 영어영역" --school "○○학원" \
    --report-type english --out output
```

#### 영어 모의고사 정답키 형식 (`examples/english_answer_key.json`)
문항별 메타데이터(영역·유형·난이도)와 절대평가 등급컷을 추가합니다.
```json
{
  "exam_id": "ENG2026M03", "subject": "english",
  "default_point": 2,
  "grade_cuts": [90, 80, 70, 60, 50, 40, 30, 20],
  "answers": {"1": 5, "...": "...", "45": 3},
  "points": {"13": 3, "21": 3},
  "question_meta": {
    "1":  {"area": "듣기", "type": "목적", "difficulty": "중"},
    "31": {"area": "독해", "type": "빈칸 추론", "difficulty": "상"}
  }
}
```
- `grade_cuts`: 절대평가 등급컷(내림차순). `90↑=1등급`, 미만이면 다음 등급.
- `question_meta.area` / `type` / `difficulty`: 영역별·유형별·난이도별 분석에 사용
  (없는 문항은 해당 집계에서 제외).

### 폰트 (Pretendard)
성적표는 **Pretendard**를 우선 사용합니다(목동유쌤 리포트 톤). 단독 HTML에는
Pretendard 동적 서브셋 CDN(`jsdelivr`) 링크가 포함되며, 온라인 배포 시 자동 적용됩니다.
오프라인/사내망 등 CDN을 못 쓰는 환경에서는 Noto Sans KR 등으로 폴백합니다
(완전 오프라인이면 Pretendard를 자체 호스팅하거나 `render_report_html(font_cdn=False)` 사용).

### 답안지 스타일 (`--style`)
- **`basic`**(기본): 세로 A4 단순형(마커·QR·학번·문항).
- **`exam`**: **수능형 가로 답안지** — 흰 배경, 상·하 타이밍 마크, 네이비 제목
  밴드(`③교시 영어 영역`), **성명 / 학교·학년 / 수강반** 인적사항 박스,
  **수험번호 그리드(왼쪽부터 4~5자리)**, 감독관 확인란, 수능식 3단 문항(20/20/5,
  5행 그룹 음영·5의 배수 강조), 분홍 버블(①~⑤), **학원 로고 이미지(우하단)**.
  제목 밴드는 `--title`을 자동 사용하며 폭에 맞춰 크기 조정("답안지" 자동 부기).
  정렬 마커·QR·판독 좌표는 동일하게 유지되어 판독 정확도는 그대로입니다.

```bash
python -m omr.cli generate --style exam \
    --exam ENG2026M03 --title "고1 3월 전국연합학력평가 · 영어영역" \
    --questions 45 --choices 5 --id-digits 5 --per-column 20 \
    --period 3 --subject "영어 영역" --academy "목동유쌤영어학원" \
    --academy-logo assets/academy-logo.png --out output
```
`--academy-logo`로 학원 로고 PNG를 지정하면 우하단에 배치됩니다(없으면 학원명 텍스트).
`--per-column 20`이면 20/20/5(수능식)로 배치되어 3단째 아래 공간에 로고가 놓입니다.
수험번호는 **왼쪽부터 채워** 표기하며(4~5자리), 판독기는 뒤쪽 빈 칸을 미기입으로
간주해 절삭합니다(예: `1234` → 5칸 중 4칸만 마킹).

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

## 웹앱 (업무용 UI)

CLI 대신 **웹 브라우저에서 전 과정을 처리**하는 FastAPI 앱(`webapp/`)이 있습니다.
OMR 핵심 로직(`omr` 패키지)을 그대로 재사용합니다.

```bash
pip install -r requirements.txt
python -m uvicorn webapp.app:app --host 0.0.0.0 --port 8000
#  → 브라우저에서 http://localhost:8000
```

업무 흐름(한 화면에서):
1. **새 시험 만들기** — 제목·유형(기본/영어)·답안지 구성 입력
2. **① 답안지(OMR) 출력** — 미리보기 + PDF 다운로드(시험 전 인쇄·배부)
3. **② 정답키 입력** — 문항별 정답 클릭(영어는 표준 유형·배점·등급컷 자동 적용)
4. **③ 스캔 판독·채점** — 마킹된 답안지 이미지 업로드 → 자동 판독·채점(석차·등급 표)
5. **④ 웹 성적표 생성** — 응시자별 링크 생성(학부모 열람·알림톡 발송용)

시험 데이터는 `data/exams/<시험코드>/`에 파일로 저장됩니다(`OMR_DATA_DIR`로 변경 가능).
학원 로고는 `assets/academy-logo.png`를 자동 사용합니다.

| 파일 | 역할 |
|------|------|
| `webapp/app.py`     | FastAPI 라우트(시험·OMR·정답키·판독·성적표) |
| `webapp/storage.py` | 시험 설정 저장 + SheetConfig/AnswerKey 파생, 영어 표준 구성 |
| `webapp/templates/` | 화면(대시보드·시험 생성·시험 관리) — 네이비 톤 |
| `webapp/static/`    | 스타일시트 |

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
2. **[완료] 웹링크 형식 성적표** — 토큰 HTML + manifest + 교사용 목록/CSV,
   네이비 톤앤매너, **기본형/영어 모의고사** 유형, Pretendard 적용
3. **학부모 알림톡** — manifest의 링크를 승인 템플릿 변수에 치환하여 발송대행사
   API로 배치 발송
   *(선행 필요: 카카오 비즈니스 채널 개설, 발송대행사 계약, 템플릿 사전 심사)*

향후 성적표 유형(국어·수학 모의고사, 학부모 요약형 등)은 `render_*_report_body`
함수를 추가하고 `report_type`에 연결하면 동일 톤으로 확장됩니다.

## 운영 팁 (정확도 극대화)
- **평판/ADF 스캐너 200~300dpi 흑백/그레이스케일** 입력을 권장합니다.
- 인쇄 시 "실제 크기(100%, 배율 맞춤 해제)"로 출력해 마커 규격을 유지하세요.
- 스마트폰 촬영도 동작하나, 정면·균일 조명·전체 마커 포함 촬영이 필요합니다.
