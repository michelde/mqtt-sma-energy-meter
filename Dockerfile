FROM python:3.12-alpine

WORKDIR /app

# Install dependencies in a separate layer for better cache reuse
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY mqtt_sma_bridge.py .

# Non-root user
RUN adduser -D appuser
USER appuser

# Log level can be overridden via environment variable or --log-level CLI arg.
# All other configuration lives in config.yaml (mount at /app/config.yaml).
ENV LOG_LEVEL=INFO

# -u: unbuffered output so Docker logs show lines immediately
ENTRYPOINT ["python", "-u", "mqtt_sma_bridge.py"]
