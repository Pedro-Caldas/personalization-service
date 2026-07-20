#!/bin/sh
# Materializa o artefato de recomendacoes (job de batch) e so entao sobe a API.
# Rodar o job aqui, e nao no build, mantem o artefato consistente com os dados
# presentes no container em cada boot.
set -e

echo "Gerando artefato de recomendacoes (job de batch)..."
python -m scripts.prepare_features --output "$ARTIFACT_PATH"

echo "Subindo a API..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
