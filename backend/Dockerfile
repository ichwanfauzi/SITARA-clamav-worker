FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY alembic.ini ./alembic.ini
COPY app ./app
COPY migrations ./migrations
RUN pip install --no-cache-dir .
USER 10001:10001
