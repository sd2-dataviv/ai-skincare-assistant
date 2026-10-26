FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY app ./app
RUN pip install .
COPY alembic.ini ./
COPY alembic ./alembic
RUN useradd -m -u 10001 api && mkdir -p /data/photos && chown -R api /data
USER api
ENV STORAGE_DIR=/data/photos
EXPOSE 8000
# SEED_DEMO_USERS=1 also creates the demo staff accounts (doctor, admin, clinic device) for a first look.
CMD ["sh", "-c", "alembic upgrade head && if [ \"$SEED_DEMO_USERS\" = 1 ]; then python -m app.seed --demo-users; else python -m app.seed; fi && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
