"""Constantes de configuracao do serving.

Apenas o que a API precisa em tempo de request. O caminho do artefato aceita
override por variavel de ambiente (util no container e nos testes).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_ARTIFACT_PATH = REPO_ROOT / "artifacts" / "features_artifact.json"
ARTIFACT_PATH = Path(os.getenv("ARTIFACT_PATH", str(_DEFAULT_ARTIFACT_PATH)))

# Quantidade de recomendacoes retornadas: default e teto maximo (protege o
# payload; acima do teto o valor e limitado, nao rejeitado).
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
