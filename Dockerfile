# OMR 채점 시스템 배포 이미지
FROM python:3.11-slim

# 한글 폰트(나눔) + OpenCV 런타임 라이브러리
# (fc-cache는 slim 이미지에 없고, 폰트는 파일 경로로 직접 로드하므로 불필요)
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-nanum libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV OMR_DATA_DIR=/data/exams
EXPOSE 8000

# 기본은 stateless OMR API(mock-report-web 연동용).
# 단독 관리 웹앱을 쓰려면 APP_MODULE=webapp.app:app 로 실행.
ENV APP_MODULE=omr_api.main:app
CMD ["sh", "-c", "python -m uvicorn ${APP_MODULE} --host 0.0.0.0 --port ${PORT:-8000}"]
