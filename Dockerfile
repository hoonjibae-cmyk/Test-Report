# OMR 채점 시스템 배포 이미지
FROM python:3.11-slim

# 한글 폰트(나눔) + OpenCV 런타임 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-nanum libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 시험 데이터는 영구 디스크(/data)에 저장 (호스팅에서 볼륨 마운트)
ENV OMR_DATA_DIR=/data/exams
EXPOSE 8000

# 호스팅이 주는 PORT 사용(없으면 8000)
CMD ["sh", "-c", "python -m uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
