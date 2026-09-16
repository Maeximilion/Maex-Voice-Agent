FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY ca-bundle.crt /usr/local/share/ca-certificates/ca-bundle.crt
RUN update-ca-certificates

COPY requirements.txt /app/requirements.txt
RUN PIP_CERT=/etc/ssl/certs/ca-certificates.crt pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
