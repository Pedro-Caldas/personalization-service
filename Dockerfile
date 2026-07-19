# Imagem unica: contem o job de batch (pipeline) e a API (serving).
# O job roda no startup do container (ver docker-entrypoint.sh) e materializa o
# artefato de recomendacoes; so entao a API sobe. Por isso o scikit-learn/pandas
# do pipeline tambem estao presentes aqui -- separar o serving num estagio slim
# sem essas libs e uma otimizacao natural registrada como proximo passo.
FROM python:3.12-slim

# Logs JSON imediatos no stdout do container; sem .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ARTIFACT_PATH=/app/artifacts/features_artifact.json

WORKDIR /app

# Dependencias primeiro: camada cacheavel, so reinstala se requirements mudar.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codigo, dados e modelo.
COPY app/ app/
COPY pipeline/ pipeline/
COPY scripts/ scripts/
COPY data/ data/
COPY model/ model/
COPY docker-entrypoint.sh .

# Usuario nao-root e diretorio gravavel para o artefato gerado no startup.
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p artifacts \
    && useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Prontidao real do servico: 503 durante o startup, 200 apos o artefato carregar.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Roda o job de batch e, so depois, sobe a API (ver docker-entrypoint.sh).
ENTRYPOINT ["./docker-entrypoint.sh"]
