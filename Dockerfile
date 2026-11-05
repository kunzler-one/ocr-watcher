FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ocrmypdf \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir watchdog requests

COPY watcher.py .

CMD ["python", "-u", "watcher.py"]
