"""Testes unitarios da derivacao de features (pipeline/features.py).

Usam fixtures pequenos e deterministicos (tests/fixtures/) com resultados
calculaveis a mao.
"""

from pathlib import Path

import pandas as pd
import pytest

from pipeline.features import (
    FEATURE_COLS,
    build_user_feature_matrix,
    compute_affinity_categories,
    compute_interactions,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def events() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "events_fixture.csv")


@pytest.fixture
def products() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "products_fixture.csv")


# --- afinidade ------------------------------------------------------------

def test_affinity_basico(events, products):
    """u_1: p_a (view+view+purchase) em eletronicos domina livros (p_c view)."""
    affinity = compute_affinity_categories(events, products)
    assert affinity["u_1"] == "eletronicos"


def test_affinity_ponderacao_inverte_contagem_simples(events, products):
    """u_2 mostra por que a ponderacao importa.

    Contagem simples: livros=4 interacoes (2x p_c + 2x p_d) vs eletronicos=2
    (add_to_cart + purchase em p_a) -> livros venceria.
    Ponderado: eletronicos=3+5=8 vs livros=4 -> eletronicos vence.
    """
    affinity = compute_affinity_categories(events, products)
    assert affinity["u_2"] == "eletronicos"


def test_affinity_desempate_alfabetico(events, products):
    """u_3: eletronicos e esporte empatam em 1 -> vence 'eletronicos' (alfabetico)."""
    affinity = compute_affinity_categories(events, products)
    assert affinity["u_3"] == "eletronicos"


def test_affinity_ignora_usuario_sem_eventos(events, products):
    """Usuario sem eventos nao aparece no resultado (vira cold start no chamador)."""
    affinity = compute_affinity_categories(events, products)
    assert "u_inexistente" not in affinity


# --- interacoes -----------------------------------------------------------

def test_interactions_conta_por_produto(events):
    interactions = compute_interactions(events)
    assert interactions["u_1"] == {"p_a": 3, "p_c": 1}
    assert interactions["u_2"] == {"p_a": 2, "p_c": 2, "p_d": 2}


# --- montagem do vetor de features ---------------------------------------

def test_feature_matrix_usuario_conhecido(events, products):
    interactions = compute_interactions(events)
    affinity = compute_affinity_categories(events, products)

    matrix = build_user_feature_matrix(
        products,
        interactions_for_user=interactions["u_1"],
        affinity_category=affinity["u_1"],
    ).set_index("product_id")

    # interactions: produto com historico vs produto sem historico.
    assert matrix.loc["p_a", "interactions"] == 3
    assert matrix.loc["p_c", "interactions"] == 1
    assert matrix.loc["p_b", "interactions"] == 0

    # user_affinity_match: 1 para a categoria de afinidade (eletronicos), 0 fora.
    assert matrix.loc["p_a", "user_affinity_match"] == 1  # eletronicos
    assert matrix.loc["p_b", "user_affinity_match"] == 1  # eletronicos
    assert matrix.loc["p_c", "user_affinity_match"] == 0  # livros

    # todas as colunas esperadas pelo modelo estao presentes.
    for col in FEATURE_COLS:
        assert col in matrix.columns


def test_feature_matrix_cold_start_neutraliza_features(products):
    """Cold start: sem interacoes e sem afinidade -> tudo 0, independe do usuario."""
    matrix = build_user_feature_matrix(
        products, interactions_for_user=None, affinity_category=None
    )
    assert (matrix["interactions"] == 0).all()
    assert (matrix["user_affinity_match"] == 0).all()
    # features estaticas do catalogo permanecem intactas.
    assert matrix["price"].tolist() == products["price"].tolist()
