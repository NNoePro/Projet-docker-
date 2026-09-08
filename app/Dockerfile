FROM python:3.12-slim AS builder
WORKDIR /install
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

FROM python:3.12-slim
#Pas de fichier .pyc
ENV PYTHONDONTWRITEBYTECODE=1
#Pour obtenir les logs avec la commande 'docker compose logs'
ENV PYTHONUNBUFFERED=1 

RUN useradd --create-home --uid 1001 appuser
COPY --from=builder /install/deps /usr/local
WORKDIR /app
COPY --chown=appuser:appuser . .
USER appuser
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
