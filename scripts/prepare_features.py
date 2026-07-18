"""Job de batch: le os CSVs + modelo e materializa o artefato de recomendacoes.

Roda uma unica vez antes da API subir (passo de startup / etapa do container).
Gera um JSON com o catalogo ranqueado por usuario conhecido + um ranking default
de cold start, para que a API sirva recomendacoes com um lookup O(1) sem carregar
o modelo em tempo de request.

Uso:
    python -m scripts.prepare_features [--data-dir DIR] [--model-dir DIR]
                                       [--output ARQUIVO]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from pipeline.features import (
    build_user_feature_matrix,
    compute_affinity_categories,
    compute_interactions,
)
from pipeline.scoring import Model, load_model, score_catalog

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_MODEL_DIR = REPO_ROOT / "model"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "features_artifact.json"


def compute_purchased_products(events: pd.DataFrame) -> dict[str, list[str]]:
    """``{user_id: [product_ids comprados]}`` -- alimenta o filtro de ja-comprados."""
    purchases = events[events["event_type"] == "purchase"]
    grouped = purchases.groupby("user_id")["product_id"].unique()
    return {user_id: sorted(pids.tolist()) for user_id, pids in grouped.items()}


def build_artifact(
    events: pd.DataFrame,
    products: pd.DataFrame,
    model: Model,
    model_version: str,
) -> dict:
    """Monta o artefato completo (usuarios conhecidos + ranking de cold start)."""
    affinity = compute_affinity_categories(events, products)
    interactions = compute_interactions(events)
    purchased = compute_purchased_products(events)

    known_users: dict[str, dict] = {}
    for user_id in sorted(events["user_id"].unique()):
        matrix = build_user_feature_matrix(
            products,
            interactions_for_user=interactions.get(user_id),
            affinity_category=affinity.get(user_id),
        )
        known_users[user_id] = {
            "affinity_category": affinity.get(user_id),
            "purchased_product_ids": purchased.get(user_id, []),
            "ranking": score_catalog(matrix, model),
        }

    # Ranking default de cold start: vetor neutro (mesmo para qualquer usuario
    # desconhecido), calculado uma unica vez.
    cold_matrix = build_user_feature_matrix(
        products, interactions_for_user=None, affinity_category=None
    )

    return {
        "model_version": model_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "known_users": known_users,
        "cold_start_ranking": score_catalog(cold_matrix, model),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    events = pd.read_csv(args.data_dir / "events.csv")
    products = pd.read_csv(args.data_dir / "products.csv")
    model = load_model(args.model_dir / "model.pkl")

    model_card = json.loads((args.model_dir / "model_card.json").read_text())
    model_version = f"{model_card['model_name']}@{model_card['version']}"

    artifact = build_artifact(events, products, model, model_version)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(
        f"Artefato gerado em {args.output}\n"
        f"  model_version:      {model_version}\n"
        f"  usuarios conhecidos: {len(artifact['known_users'])}\n"
        f"  produtos no catalogo: {len(products)}\n"
        f"  itens no cold start:  {len(artifact['cold_start_ranking'])}"
    )


if __name__ == "__main__":
    main()
