FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pm5 ./pm5
COPY run.py .
COPY dashboard.py .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PM_LIVE_STATUS=false \
    PM_RECORD=true \
    PM_DATA_FILE=data/trades.jsonl

RUN mkdir -p data

CMD ["python", "-u", "run.py"]
