FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every module in the build context, not a hand-listed few. `.gcloudignore`
# already decides what reaches the context -- it excludes admin.py, the venv,
# every *.json and .env -- so naming files again here only duplicated that
# judgement in a second place, where it then drifted: telemetry.py was added to
# the service and never added to this line, and the first deploy died with
# ModuleNotFoundError after a full container build. A missing module is a total
# outage that nothing catches until the revision fails to start.
COPY *.py prompts.yaml ./

# Cloud Run injects PORT. One worker with several threads is the right shape
# here: every request spends nearly all its time waiting on Vertex AI, so
# threads cost nothing and processes would just multiply the cold-start memory.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1
