FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GRADIO_ANALYTICS_ENABLED=False
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src src
COPY frontend frontend
COPY scripts scripts
RUN mkdir -p /app/data && useradd --uid 10001 --create-home planner && chown -R planner:planner /app
USER planner
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
