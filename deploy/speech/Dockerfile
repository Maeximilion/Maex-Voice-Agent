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

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Ear: Whisper through CTranslate2, Parakeet through ONNX. Mouth: Piper.
# The two nvidia packages are the CUDA libraries CTranslate2 loads on a GPU.
RUN pip install --no-cache-dir \
        faster-whisper==1.2.1 \
        "onnx-asr[cpu,hub]==0.12.0" \
        piper-tts==1.8.0 \
        nvidia-cublas-cu12==12.9.2.10 \
        nvidia-cudnn-cu12==9.27.0.42
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
