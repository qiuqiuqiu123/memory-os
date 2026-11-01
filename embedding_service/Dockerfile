FROM python:3.12.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
WORKDIR /app
# Install the CPU wheel explicitly; PyPI's default torch can pull CUDA libraries.
RUN pip install --no-deps --index-url https://download.pytorch.org/whl/cpu torch==2.6.0+cpu
COPY requirements.txt ./
RUN pip install --index-url ${PIP_INDEX_URL} -r requirements.txt \
    && useradd --create-home --uid 10001 embedding \
    && mkdir -p /models/huggingface && chown -R embedding:embedding /models
COPY --chown=embedding:embedding app.py download_model.py ./
USER embedding
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
