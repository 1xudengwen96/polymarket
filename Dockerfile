FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY pm5hft ./pm5hft
COPY config ./config
COPY artifacts ./artifacts

RUN pip install --no-cache-dir .

CMD ["python", "-m", "pm5hft.main"]
