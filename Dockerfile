FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Chave temporaria apenas para o collectstatic do build (nao e usada em runtime).
RUN DJANGO_SECRET_KEY=build-only-nao-usar python manage.py collectstatic --noinput \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/media \
    && chown -R app:app /app /data

USER app
EXPOSE 8000
CMD ["sh", "-c", "gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2"]
