FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ENV TIKTOKEN_CACHE_DIR=/app/memory_os/tokenizer_cache MEM0_TELEMETRY=false
WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && useradd --create-home --uid 10001 memory
COPY --chown=memory:memory . .
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
USER memory
EXPOSE 8000
CMD ["uvicorn", "memory_os.app:app", "--host", "0.0.0.0", "--port", "8000"]
