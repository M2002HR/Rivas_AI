FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY submodules /app/submodules

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir /app/submodules/mira-telegram-service && \
    pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["rivas"]
