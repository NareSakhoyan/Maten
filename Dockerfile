FROM python:3.12-slim

ARG INSTALL_PIE=true
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements-pie.txt ./
COPY app ./app
COPY alembic ./alembic
COPY config ./config
COPY resources ./resources
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .
RUN if [ "$INSTALL_PIE" = "true" ]; then \
        python -m venv /opt/pie-runtime \
        && /opt/pie-runtime/bin/pip install --no-cache-dir --extra-index-url "$TORCH_CPU_INDEX_URL" -r requirements-pie.txt \
        && /opt/pie-runtime/bin/pip install --no-cache-dir --no-deps nlp-pie==0.3.8; \
    fi

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
