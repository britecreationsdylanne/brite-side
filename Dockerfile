FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# gthread workers (2 workers x 8 threads = 16 concurrent) so a long, I/O-bound
# Claude or SendGrid call doesn't block every other request the way 2 sync
# workers did. Timeout is aligned with the Claude client's 90s request timeout.
CMD exec gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gthread --workers 2 --threads 8 --timeout 120
