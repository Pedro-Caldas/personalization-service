"""Testes unitarios do pos-processamento de ranking (app/ranking.py)."""

import pytest

from app.config import DEFAULT_LIMIT, MAX_LIMIT
from app.ranking import apply_purchase_filter, build_recommendations, resolve_limit


def make_ranking(product_ids: list[str]) -> list[dict]:
    """Ranking sintetico ja ordenado por score desc."""
    return [
        {
            "product_id": pid,
            "score": round(1.0 - i * 0.1, 6),
            "category": "eletronicos",
            "price": 10.0 + i,
        }
        for i, pid in enumerate(product_ids)
    ]


# --- resolve_limit --------------------------------------------------------

def test_resolve_limit_default():
    assert resolve_limit(None) == DEFAULT_LIMIT


def test_resolve_limit_customizado():
    assert resolve_limit(25) == 25


def test_resolve_limit_acima_do_teto_e_limitado():
    assert resolve_limit(100) == MAX_LIMIT


@pytest.mark.parametrize("invalido", [0, -1, -50])
def test_resolve_limit_rejeita_invalido(invalido):
    with pytest.raises(ValueError):
        resolve_limit(invalido)


# --- apply_purchase_filter ------------------------------------------------

def test_filtro_remove_ja_comprados():
    ranking = make_ranking(["p1", "p2", "p3"])
    selected, applied = apply_purchase_filter(ranking, ["p2"])
    assert [i["product_id"] for i in selected] == ["p1", "p3"]
    assert applied is True


def test_filtro_sem_compras_mantem_tudo_e_flag_true():
    """Usuario sem compras: nada a remover, mas o filtro esta ativo."""
    ranking = make_ranking(["p1", "p2"])
    selected, applied = apply_purchase_filter(ranking, [])
    assert [i["product_id"] for i in selected] == ["p1", "p2"]
    assert applied is True


def test_filtro_fallback_quando_comprou_catalogo_inteiro():
    """Caso extremo: comprou tudo -> devolve catalogo completo, flag False."""
    ranking = make_ranking(["p1", "p2"])
    selected, applied = apply_purchase_filter(ranking, ["p1", "p2"])
    assert [i["product_id"] for i in selected] == ["p1", "p2"]
    assert applied is False


# --- build_recommendations ------------------------------------------------

def test_build_atribui_rank_e_respeita_limit():
    ranking = make_ranking(["p1", "p2", "p3", "p4"])
    items, applied = build_recommendations(ranking, purchased_product_ids=[], limit=2)
    assert [i["product_id"] for i in items] == ["p1", "p2"]
    assert [i["rank"] for i in items] == [1, 2]
    assert applied is True


def test_build_filtra_depois_aplica_limit_e_rank():
    ranking = make_ranking(["p1", "p2", "p3", "p4"])
    items, applied = build_recommendations(
        ranking, purchased_product_ids=["p1"], limit=2
    )
    assert [i["product_id"] for i in items] == ["p2", "p3"]
    assert [i["rank"] for i in items] == [1, 2]
    assert applied is True


def test_build_preserva_campos_do_item():
    ranking = make_ranking(["p1"])
    items, _ = build_recommendations(ranking, purchased_product_ids=[], limit=10)
    item = items[0]
    assert set(item) == {"product_id", "score", "category", "price", "rank"}
