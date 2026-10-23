FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 1000 --create-home hub
COPY hub ./hub
COPY web ./web
COPY registry.json settings.example.json ./
COPY *.py ./
USER 1000:1000
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4)"
CMD ["python", "-m", "hub.server"]
