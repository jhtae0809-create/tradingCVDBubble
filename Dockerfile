# Dashboard-only image: serves whatever is already in MongoDB.
#
# It does NOT collect data. The collector needs IB Gateway logged in with a
# market-data subscription, which is a second, much heavier service (see
# deploy/README.md); this image is the part that can be handed to someone as a
# URL and just work.
FROM python:3.12-slim

# - PYTHONUNBUFFERED: without it the platform's log view stays empty until a
#   buffer fills, so a crash at startup looks like silence.
# - PYTHONUTF8: this project logs arrows, box characters and em dashes; on a
#   non-UTF-8 default locale print() raises UnicodeEncodeError and logging
#   silently drops the record (the same failure the Windows CI covers).
# - DASH_DEBUG: never the Werkzeug debugger on a public URL. gunicorn does not
#   read this, but it keeps a hand-run `python -m app` in this image safe too.
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DASH_DEBUG=0 \
    HOST=0.0.0.0

WORKDIR /app

# Requirements first, so the dependency layer is cached and only reinstalled
# when the requirement files themselves change.
COPY requirements-demo.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY . .

EXPOSE 8050
CMD ["python", "deploy/railway_start.py"]
