FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY sync.py healthcheck.py ./
RUN useradd -r -u 10001 xpsync
USER xpsync
CMD ["python", "sync.py"]
