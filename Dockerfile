# GTM Engine pipeline runtime image. Python 3.12 to match the Windmill workers.
#
#   docker build -t gtm-engine .
#   docker run --rm gtm-engine                       # keyless demo (no real sends)
#   docker run --rm --env-file .env gtm-engine python -m scripts.run_pipeline
#
# Runtime deps only — pytest / respx / deepeval are dev/test-only and live in
# requirements.txt (installed in CI, not baked into the runtime image).
FROM python:3.12-slim

WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY scripts/ ./scripts/

# Default command: run the full funnel keyless with synthetic leads (smoke / dry
# run). Override for a real run, e.g. `python -m scripts.run_pipeline`.
CMD ["python", "-m", "scripts.run_pipeline", "--demo"]
