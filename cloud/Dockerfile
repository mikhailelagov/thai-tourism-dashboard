FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.58.0 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY probe.py /app/probe.py
CMD ["python", "/app/probe.py"]
