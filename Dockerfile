FROM python:3.11-slim

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY app.py database.py paths.py server.py ./
COPY templates ./templates
COPY config.example.json ./config.json

ENV TIMECHECKER_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Railway injects $PORT; gunicorn binds to it.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 60 server:flask_app"]
