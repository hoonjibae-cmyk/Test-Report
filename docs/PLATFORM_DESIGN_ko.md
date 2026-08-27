# OMR 성적표 플랫폼 — 설계 확정 문서 (v0.1)

목동유쌤영어학원용. mock-report-web(Next.js+Supabase, Vercel)의 **톤앤매너·인프라를
계승**하고, 여기에 **OMR 자동 판독**과 **자유로운 시험 유형/성적표**를 얹는다.

---

## 1. 아키텍처 (하이브리드)

```
[학부모/교사] ── Vercel (Next.js: mock-report-web 확장)
                       │  계정·시험관리·성적표 저장/공유·AI·디자인 톤
             ┌─────────┴───────────┐
        [Supabase]            [OMR API (Python)]  ← Render 배포(무상태)
   DB · Storage(스캔,7일)      답안지 PDF 생성 + 스캔 판독(OpenCV)
```

- **Next 앱**: 화면·계정·데이터·성적표 렌더링·AI·공유링크(PIN) — 기존 자산 재활용.
- **OMR API**: 이미지 처리 전담(무상태). 이미 구축·검증 완료(`omr_api/`), Render 배포 대기.
- 연결 환경변수(Next): `OMR_API_URL`, `OMR_API_KEY`.

---

## 2. 시험 유형 & 성적표 매핑

| 시험 유형 | 문항수 | 성적표 계열 | 담임 의견 |
|-----------|--------|-------------|:--------:|
| 국영수 모의고사 | 고정(국45/수30/영45) | **Ⓐ 리치형(기존 파이프라인)** — 등급·유형·전국비교·AI | 선택 |
| 토요모의고사(영어) | 45 | **Ⓑ 영어형** — 절대평가 등급·듣기/독해·유형별·집단평균 | 선택 |
| 월말평가 | 유저 선택 | **Ⓒ 범용 리치형** — 점수·석차·백분위·영역/난이도·집단평균·문항별 | 선택(주로 사용) |
| 반배치고사 | 유저 선택 | Ⓒ 범용 리치형 | 선택 |
| 인클래스 테스트 | 유저 선택 | Ⓒ 범용 리치형 | 선택 |

- **담임 의견은 모든 유형에서 on/off** 가능(시험 생성 시 체크).
- 세 계열 모두 **동일한 네이비+골드 톤**을 공유(§8). Ⓒ 목업은 발행된 아티팩트 참조.

---

## 3. 데이터 모델 (Supabase 추가/변경)

기존 3테이블(`app_users`, `report_batches`, `student_reports`)은 유지. 아래를 추가.

### 3.1 새 테이블 `exams`
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | uuid PK | |
| exam_type | text | `mock`\|`saturday`\|`monthly`\|`placement`\|`inclass` |
| report_family | text | `A_rich`\|`B_english`\|`C_generic` (유형에서 파생) |
| title | text | 시험 제목(성적표 표기) |
| subject | text | `english` 등(범용은 자유 라벨) |
| num_questions | int | |
| num_choices | int | 기본 5 |
| id_digits | int | 수험번호 자리(기본 5, 왼쪽정렬) |
| omr_style | text | `exam`(수능형)\|`basic` |
| omr_config | jsonb | period·subject_label·per_column 등 OMR 설정 |
| answer_key | jsonb | `{문항: 정답보기}` (Ⓑ/Ⓒ). Ⓐ는 기존 `exams.json` 사용 |
| points | jsonb | 문항별 배점(기본 균등) |
| question_meta | jsonb | 문항별 `{area,type,difficulty}` (교사 태그, 선택) |
| grade_cuts | jsonb | 절대평가 등급컷(선택; Ⓑ 기본 제공) |
| use_teacher_comment | bool | 담임 의견 사용 여부 |
| created_by | text | |
| created_at | timestamptz | |

> Ⓐ(국영수)는 정답키·분류가 이미 `data/exams.json`에 있으므로 `answer_key` 등을 비워도 됨.

### 3.2 `student_reports` (기존 재활용 + 소폭 확장)
- 범용 성적표도 `report_data jsonb`에 저장(스키마 v2). 공유 `public_token`·`access_pin_hash` 그대로.
- 컬럼 추가: `exam_id uuid → exams(id)`, `scan_path text`(Storage 경로), 
  `teacher_comment jsonb`(`{keywords[], ai_draft, final, edited_by, updated_at}`).

### 3.3 Storage 버킷 `omr-scans`
- 업로드된 스캔 원본 저장. **7일 후 자동 삭제**(Supabase Storage 수명주기 또는 일일 정리 크론).
- 재판독: 보관 기간 내 `scan_path`로 다시 `/read` 호출.

---

## 4. 화면 흐름

**교사(관리자)**
1. **시험 만들기** — 유형 선택 → (유형별) 문항수·보기수·정답키·교사태그·등급컷·담임의견 on/off
2. **답안지 출력** — OMR API `/generate` → PDF 다운로드(인쇄·배부)
3. **스캔 업로드** — 이미지 업로드 → Storage 저장 → `/read` → 학번·문항답 표
4. **검수** — 이중표기/미인식 문항 확인·수정(플래그 표시)
5. **성적표 생성** — 유형별 계열로 렌더·저장(공유 링크 생성)
6. **담임 의견**(사용 시) — 학생별 키워드 입력 → **AI 초안** → 교사 첨삭 → 최종 저장

**학부모/학생**
- 링크 접속(필요 시 PIN=전화 뒤 4자리) → 성적표 열람

---

## 5. OMR API 계약 (구축 완료)

- `GET /health`
- `POST /generate` — body: 시트 설정(JSON) → `{template, pdf_base64, filename}`
- `POST /read` — multipart: `spec`(시트설정) **또는** `template` + `files`(이미지)
  → `{results:[{filename, student_id, answers{문항:보기|null}, review_flags, exam_id}], problems:[]}`
- 인증 헤더 `X-API-Key`. CORS 허용 도메인 `ALLOWED_ORIGINS`.

Next는 시험 생성 시의 **작은 설정(spec)** 만 `exams`에 저장하면, 생성·판독 모두 그 설정으로 처리.

---

## 6. 담임 의견 워크플로우

1. 교사가 학생별 **키워드** 입력(예: `#어휘성실 #독해속도보완 #숙제우수`)
2. 서버가 **키워드 + 성적표 데이터**를 OpenAI에 전달 → **피드백 초안** 생성
   (기존 `lib/ai.ts` 패턴 재활용; `OPENAI_API_KEY` 없으면 규칙기반 초안)
3. 교사가 초안을 화면에서 **수정·첨삭** → **최종본 저장**(`teacher_comment.final`)
4. 성적표에 최종본 표시(“AI 초안 기반 · 담임 첨삭 완료” 배지 + 담임 서명)

- 상태: `keywords → ai_draft → final`. 최종 저장 전까지는 성적표에 미노출(옵션).

---

## 7. 스캔 이미지 보관 (7일)

- 업로드 즉시 `omr-scans` 버킷에 저장, `student_reports.scan_path` 기록.
- 보관 7일: Storage 수명주기 규칙 또는 매일 크론이 `created_at < now()-7d` 삭제.
- 재판독 UI: 보관 기간 내 “다시 판독” → `/read` 재호출로 결과 갱신.

---

## 8. 성적표 템플릿 (톤 계승)

- 공통 토큰: navy `#183c73` · blue `#2f67b1` · gold `#d2a93b` · 흰 배경 · 성공/위험 시맨틱.
- 공통 요소: 문서 카드(상단 네이비 라인) · 학생 정보 스트립 · 원점수 강조 KPI ·
  네이비→블루 그라디언트 막대(**집단평균 금색 마커**) · ○/×/– 히트맵 · 담임 의견 카드(골드).
- 계열별 차이:
  - Ⓐ 리치형: 등급·전국비교·영역/유형/난이도·AI 총평(기존 컴포넌트).
  - Ⓑ 영어형: 절대평가 등급 배지·듣기/독해·독해 대분류·난이도.
  - Ⓒ 범용형: 교사 태그 기반 영역/난이도·문항별·(선택)담임 의견. **문항수 가변**.
- 구현: Next의 React 컴포넌트로 작성(공유·PIN·인쇄 인프라 재활용). 본 문서의 목업이 시각 계약.

---

## 9. 단계별 로드맵

| 단계 | 내용 | 산출물 |
|------|------|--------|
| **A. 기반+범용** | OMR API 배포 · `exams`/Storage · 시험생성·답안지·스캔·검수 · **Ⓒ 범용 리치 성적표** | 월말/반배치/인클래스 실사용 |
| **B. 담임 의견** | 키워드→AI초안→첨삭→최종, 성적표 노출 | 모든 유형 선택 적용 |
| **C. 리치/영어형** | Ⓐ 국영수 OMR 연동 · Ⓑ 토요모의고사(영어) 템플릿 이식 | 5개 유형 전부 지원 |
| **D. 마무리** | 7일 보관 자동화 · 엑셀 병행 · 내보내기(PDF/CSV) · 권한 정리 | 운영 안정화 |

---

## 10. 결정/확인 필요

- [ ] Ⓒ 범용형 성적표 세부: 위 목업 구성 확정? 빼거나 추가할 항목?
- [ ] 담임 의견 AI: 사용 모델/톤(문체·길이) 기본값. `OPENAI_API_KEY` 제공 여부.
- [ ] 국영수 Ⓐ: 기존 `exams.json` 정답키를 그대로 사용(수학 단답형은 v2)?
- [ ] 구현 착수 시 mock-report-web **쓰기 권한 연결 + PR** 진행 동의.
</content>
