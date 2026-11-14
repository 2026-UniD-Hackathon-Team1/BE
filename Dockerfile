FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=staging
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser alembic.ini .
USER appuser
CMD ["python", "-m", "backend.serve"]
