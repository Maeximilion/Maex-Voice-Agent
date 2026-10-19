FROM python:3.12-slim AS deps

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
    && pip install --no-cache-dir --require-hashes -r /app/requirements.txt

# The service `voice` (docs/20 §2): the API's image plus the speech engines of
# the test phase, about 3 GB more, most of it the CUDA libraries. Its own
# target, so `api`, `dispatcher` and the drills do not carry them.
FROM deps AS voice
COPY requirements-voice.txt /app/
RUN if [ -f /app/ca-bundle.crt ]; then \
        export PIP_CERT=/etc/ssl/certs/ca-certificates.crt; \
    fi \
    && pip install --no-cache-dir --require-hashes -r /app/requirements-voice.txt
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
COPY . /app/api
EXPOSE 8001
CMD ["uvicorn", "--factory", "api.telephony.app:from_settings", "--host", "0.0.0.0", "--port", "8001"]

# Last, so a build without a target is the API's image.
FROM deps AS api
COPY . /app/api
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
