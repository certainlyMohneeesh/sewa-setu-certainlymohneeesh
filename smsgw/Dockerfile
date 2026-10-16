FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir Flask==2.2.5 Werkzeug==2.2.3 gunicorn==20.1.0
COPY app.py .
EXPOSE 8025
# Exactly one worker process: the message store lives in memory, so a second process
# would have its own, different inbox. Threads give concurrency within it.
CMD ["gunicorn", "--bind", "0.0.0.0:8025", "--workers", "1", "--threads", "16", "--timeout", "30", "app:app"]
