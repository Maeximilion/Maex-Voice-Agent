# Spike T-10.3 (docs/20 §6, §10): the open speech engines of the test phase next
# to the project's own packages, so one container speaks, hears and judges.
# Built and run by scripts/speech_spike.py; build context is api/.
# Not part of the product stack: the image of the service `voice` comes with T-1.11.
#
# Python 3.12 on purpose: the telephone band of the bench uses `audioop`, which
# left the standard library in 3.13.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Every package by hash, as the image of `voice` installs them (api/Dockerfile,
# #252 finding 13): the project's, then ear and mouth with the CUDA libraries
# CTranslate2 loads on a GPU, so the spike measures the versions that ship.
COPY requirements.txt requirements-voice.txt /app/
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt \
    && pip install --no-cache-dir --require-hashes -r /app/requirements-voice.txt

# Parakeet through ONNX, measured here only, so in neither lock file. Its
# dependencies (numpy, onnxruntime, huggingface-hub) come hashed from the
# voice file above; the hash is that of the py3-none-any wheel on PyPI.
RUN echo "onnx-asr==0.12.0 --hash=sha256:5e7ceca454609819ea7833f61e2302e0c8f6ece4f8a78b66c5daba53cb51de4a" > /tmp/onnx-asr.txt \
    && pip install --no-cache-dir --require-hashes --no-deps -r /tmp/onnx-asr.txt \
    && pip check
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
