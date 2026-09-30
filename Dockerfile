FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY smcbot ./smcbot
COPY config.yaml .

RUN mkdir -p state logs data backtests

# The engine touches state/heartbeat every monitor tick (15 s); unhealthy if silent for 10 min.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('state/heartbeat')<600 else 1)"

CMD ["python", "-m", "smcbot", "run"]
