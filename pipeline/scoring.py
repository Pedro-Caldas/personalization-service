"""Carrega o modelo treinado e ranqueia o catalogo para um perfil de usuario.

Junto com scripts/prepare_features.py, e o unico ponto que depende de
scikit-learn: apenas o job de batch roda o modelo. A API consome o artefato
ja pronto e nunca importa este modulo.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class Model:
    """Conteudo do model.pkl: estimador, scaler e ordem esperada das features."""

    estimator: Any
    scaler: Any
    feature_cols: list[str]


def load_model(model_path: str | Path) -> Model:
    """Carrega o artefato serializado (dict com model, scaler, feature_cols)."""
    with open(model_path, "rb") as f:
        artifact = pickle.load(f)
    return Model(
        estimator=artifact["model"],
        scaler=artifact["scaler"],
        feature_cols=list(artifact["feature_cols"]),
    )


def score_catalog(feature_matrix: pd.DataFrame, model: Model) -> list[dict]:
    """Ranqueia o catalogo inteiro para uma matriz de features de usuario.

    Escala as features (na ordem exata de ``model.feature_cols``, fonte da
    verdade que pega desalinhamento de coluna) e usa ``predict_proba`` para a
    probabilidade de compra (classe positiva). Retorna a lista ordenada por
    score desc, com desempate por ``product_id`` para ranking reprodutivel.

    Cada item: ``{product_id, score, category, price}``.
    """
    features = feature_matrix[model.feature_cols].to_numpy()
    scaled = model.scaler.transform(features)
    proba = model.estimator.predict_proba(scaled)[:, 1]

    ranked = feature_matrix.assign(score=proba).sort_values(
        ["score", "product_id"], ascending=[False, True]
    )
    return [
        {
            "product_id": row.product_id,
            "score": round(float(row.score), 6),
            "category": row.category,
            "price": float(row.price),
        }
        for row in ranked.itertuples(index=False)
    ]
