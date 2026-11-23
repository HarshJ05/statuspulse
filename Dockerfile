FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY statuspulse ./statuspulse
COPY monitors.toml .
ENV RUN_SCHEDULER=1 STATUSPULSE_DB=/app/data/statuspulse.db
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn statuspulse.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
