"""Funcoes puras de feature engineering.

Sem I/O: recebem DataFrames ja carregados e devolvem estruturas em memoria.
A leitura dos CSVs e a escrita do artefato ficam em scripts/prepare_features.py.
Este modulo depende apenas de pandas.
"""

from __future__ import annotations

import pandas as pd

# Pesos do funil de intencao de compra usados na derivacao de afinidade: um
# ``purchase`` e sinal muito mais forte de interesse do que um ``view``.
EVENT_WEIGHTS: dict[str, int] = {
    "view": 1,
    "click": 2,
    "add_to_cart": 3,
    "purchase": 5,
}

# Ordem exata das features esperada pelo modelo (ver model/model_card.json).
FEATURE_COLS: list[str] = [
    "interactions",
    "price",
    "avg_rating",
    "popularity_score",
    "user_affinity_match",
]


def compute_affinity_categories(
    events: pd.DataFrame, products: pd.DataFrame
) -> dict[str, str]:
    """Categoria de maior afinidade de cada usuario.

    Para cada ``user_id``, soma os pesos de ``EVENT_WEIGHTS`` por categoria de
    produto (join com ``products`` por ``product_id``) e escolhe a categoria de
    maior score ponderado. Desempate: ordem alfabetica da categoria
    (deterministico).

    Tipos de evento fora de ``EVENT_WEIGHTS`` recebem peso 0 (nao contribuem).

    Retorna ``{user_id: categoria}``. Usuarios sem nenhum evento simplesmente
    nao aparecem no dict -- o chamador trata isso como cold start.
    """
    joined = events.merge(
        products[["product_id", "category"]], on="product_id", how="inner"
    )
    joined = joined.assign(weight=joined["event_type"].map(EVENT_WEIGHTS).fillna(0))

    scores = joined.groupby(["user_id", "category"], as_index=False)["weight"].sum()
    # Vencedora por usuario: maior peso; empate resolvido por categoria asc.
    scores = scores.sort_values(
        ["user_id", "weight", "category"], ascending=[True, False, True]
    )
    winners = scores.drop_duplicates(subset="user_id", keep="first")
    return dict(zip(winners["user_id"], winners["category"]))


def compute_interactions(events: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Contagem de interacoes por (usuario, produto).

    Retorna ``{user_id: {product_id: contagem}}`` -- todas as linhas de eventos
    contam igualmente 1. A feature ``interactions`` do modelo e volume bruto de
    interacao (sem ponderacao; a ponderacao por tipo de evento vale so para a
    afinidade).
    """
    counts = events.groupby(["user_id", "product_id"]).size()
    result: dict[str, dict[str, int]] = {}
    for (user_id, product_id), count in counts.items():
        result.setdefault(user_id, {})[product_id] = int(count)
    return result


def build_user_feature_matrix(
    products: pd.DataFrame,
    interactions_for_user: dict[str, int] | None = None,
    affinity_category: str | None = None,
) -> pd.DataFrame:
    """Monta a matriz de features de um usuario (uma linha por produto).

    Parte do catalogo (``products``) e adiciona as duas features que dependem do
    usuario:

    - ``interactions``: contagem de interacoes com cada produto (0 se ausente).
    - ``user_affinity_match``: 1 se a categoria do produto e a de afinidade do
      usuario, senao 0.

    Cold start: chamar com ``interactions_for_user=None`` (ou vazio) e
    ``affinity_category=None`` neutraliza ambas as features (tudo 0), deixando o
    modelo ranquear so por price/avg_rating/popularity_score.

    As colunas de ``FEATURE_COLS`` ficam disponiveis para o scoring; as colunas
    de metadado do catalogo (product_id, category, ...) sao preservadas para
    montar o artefato de recomendacoes.
    """
    interactions_for_user = interactions_for_user or {}
    matrix = products.copy()
    matrix["interactions"] = (
        matrix["product_id"].map(interactions_for_user).fillna(0).astype(int)
    )
    if affinity_category is None:
        matrix["user_affinity_match"] = 0
    else:
        matrix["user_affinity_match"] = (
            matrix["category"] == affinity_category
        ).astype(int)
    return matrix
