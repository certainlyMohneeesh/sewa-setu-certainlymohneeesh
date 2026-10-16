FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Acknowledgment slips need the bundled Noto fonts (Assamese/Bengali, Devanagari)
# and the HarfBuzz shaping engine; fail the build early if either is missing.
RUN test -s fonts/NotoSans-Regular.ttf -a -s fonts/NotoSansBengali-Regular.ttf \
         -a -s fonts/NotoSansDevanagari-Regular.ttf \
    && python -c "import uharfbuzz, fpdf"

RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && crontab /app/deploy/crontab

EXPOSE 8000
# 4 processes x 4 threads. Each process holds a pool of at most 20 database
# connections (see app.py), so the web tier stays well under Postgres's limit.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "4", "--timeout", "30", "--worker-tmp-dir", "/dev/shm", "app:app"]
