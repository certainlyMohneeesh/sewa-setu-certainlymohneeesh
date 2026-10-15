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
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "30", "app:app"]
