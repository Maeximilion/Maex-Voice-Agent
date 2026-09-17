FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ca-bundle.crt ist optional (Builds hinter einem TLS-Proxy). Das Glob neben einer
# Pflichtdatei lässt COPY auch dann durchlaufen, wenn die Datei fehlt.
COPY requirements.txt ca-bundle.crt* /app/
RUN if [ -f /app/ca-bundle.crt ]; then \
        cp /app/ca-bundle.crt /usr/local/share/ca-certificates/extra-ca.crt \
        && update-ca-certificates \
        && export PIP_CERT=/etc/ssl/certs/ca-certificates.crt; \
    fi \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/api

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
