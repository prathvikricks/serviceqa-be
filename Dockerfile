FROM python:3.12-slim

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pip and setuptools ship in the base image and are both flagged by the
# scanner — patch them before installing deps.
RUN pip install --no-cache-dir --upgrade pip setuptools

# Dependencies first so code edits don't bust the pip layer.
COPY requirements.txt .
# Runtime needs none of pip's machinery once deps are installed. Uninstalling
# pip in the same layer drops its vendored bundle (pip/_vendor), which is the
# only place Trivy still finds vulnerable msgpack 1.1.2 / setuptools 70.3.0 —
# the real installed setuptools (84.0.0) is already patched and stays.
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y pip

COPY . .

RUN mkdir -p /app/instance && \
    adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/api/v1/health || exit 1

ENTRYPOINT ["sh", "entrypoint.sh"]
