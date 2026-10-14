FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Python dependencies (own layer, so code changes don't reinstall them)
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 2. Project files (.dockerignore keeps out .env, the virtualenv, the database and research/)
COPY . /app/
RUN python manage.py collectstatic --noinput

# 3. Run as a non-root user
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["gunicorn", "spotter_api.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "90"]
