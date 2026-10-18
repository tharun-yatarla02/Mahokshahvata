# syntax=docker/dockerfile:1
# Mahokshahvata app image: FastAPI + the background market-data thread.
# Built for linux/amd64 (the VM) and linux/arm64 (Apple Silicon laptops) as one
# tag, so the laptop and the VM run the same pushed artifact.
#
# No secrets are copied in (.dockerignore keeps .env and key files out). On the
# VM the app reads its keys from Secret Manager at startup and keeps all state
# in Cloud SQL and Cloud Storage, so the container holds nothing worth keeping.

# --- build: install dependencies into a venv (pip caches stay in this stage) ---
FROM python:3.12-slim AS build
RUN python -m venv /venv
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
 # Trim what the running app never uses: pip itself and the test suites pandas/numpy ship (~50 MB).
 && /venv/bin/pip uninstall -y -q pip \
 && rm -rf /venv/lib/python3*/site-packages/pandas/tests /venv/lib/python3*/site-packages/numpy/*/tests

# --- runtime: slim base + the venv + the code, running as a non-root user ---
FROM python:3.12-slim
ENV PATH=/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# /data: only used on laptops (DATA_DIR, a local stand-in for the storage bucket).
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
COPY --from=build /venv /venv
WORKDIR /app
# Code stays root-owned (read-only to the app user).
COPY . .
USER app
EXPOSE 8000
# Startup takes 60-90 s on the e2-micro (pandas import + market snapshot), hence the long start period.
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
# One worker: more would each start the market-data thread.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
