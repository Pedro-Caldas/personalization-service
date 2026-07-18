"""Artefato de recomendacoes pre-computado: carga e lookup por usuario.

A API carrega este artefato no startup e serve recomendacoes com um lookup O(1),
sem carregar o modelo nem importar o pipeline em tempo de request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_KEYS = {"model_version", "generated_at", "known_users", "cold_start_ranking"}


@dataclass(frozen=True)
class UserContext:
    """Resultado do lookup: base para o pos-processamento de ranking."""

    ranking: list[dict]
    purchased_product_ids: list[str]
    cold_start: bool
    affinity_category: str | None


class Artifact:
    """Encapsula o artefato carregado e resolve o contexto de cada usuario."""

    def __init__(self, data: dict):
        self.model_version: str = data["model_version"]
        self.generated_at: str = data["generated_at"]
        self._known_users: dict[str, dict] = data["known_users"]
        self._cold_start_ranking: list[dict] = data["cold_start_ranking"]

    def lookup(self, user_id: str) -> UserContext:
        """Contexto de um usuario: personalizado se conhecido, cold start senao.

        Usuario desconhecido recebe sempre o mesmo ranking default de cold start,
        independentemente do ``user_id``.
        """
        user = self._known_users.get(user_id)
        if user is None:
            return UserContext(
                ranking=self._cold_start_ranking,
                purchased_product_ids=[],
                cold_start=True,
                affinity_category=None,
            )
        return UserContext(
            ranking=user["ranking"],
            purchased_product_ids=user["purchased_product_ids"],
            cold_start=False,
            affinity_category=user["affinity_category"],
        )


def load_artifact(path: str | Path) -> Artifact:
    """Le e valida o artefato JSON gerado pelo job de batch."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = _REQUIRED_KEYS - raw.keys()
    if missing:
        raise ValueError(f"artefato invalido, faltam chaves: {sorted(missing)}")
    return Artifact(raw)
