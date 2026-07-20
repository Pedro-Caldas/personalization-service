"""Testes unitarios do cold start no nivel do artefato (app/artifact.py)."""

import json

import pytest

from app.artifact import Artifact, load_artifact

SAMPLE = {
    "model_version": "purchase_propensity_v1@1.0.0",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "known_users": {
        "u_known": {
            "affinity_category": "livros",
            "purchased_product_ids": ["p1"],
            "ranking": [
                {"product_id": "p1", "score": 0.9, "category": "livros", "price": 10.0}
            ],
        }
    },
    "cold_start_ranking": [
        {"product_id": "p2", "score": 0.5, "category": "casa", "price": 20.0},
        {"product_id": "p3", "score": 0.4, "category": "moda", "price": 30.0},
    ],
}


def test_lookup_usuario_conhecido():
    ctx = Artifact(SAMPLE).lookup("u_known")
    assert ctx.cold_start is False
    assert ctx.affinity_category == "livros"
    assert ctx.purchased_product_ids == ["p1"]
    assert ctx.ranking[0]["product_id"] == "p1"


def test_lookup_usuario_desconhecido_e_cold_start():
    ctx = Artifact(SAMPLE).lookup("nao_existe")
    assert ctx.cold_start is True
    assert ctx.purchased_product_ids == []
    assert ctx.affinity_category is None
    assert ctx.ranking == SAMPLE["cold_start_ranking"]


def test_cold_start_identico_independente_do_user_id():
    art = Artifact(SAMPLE)
    a = art.lookup("desconhecido_A")
    b = art.lookup("desconhecido_B")
    assert a.ranking == b.ranking == SAMPLE["cold_start_ranking"]
    assert a.cold_start and b.cold_start


def test_load_artifact_valida_chaves_obrigatorias(tmp_path):
    incompleto = tmp_path / "bad.json"
    incompleto.write_text(json.dumps({"model_version": "x"}))
    with pytest.raises(ValueError):
        load_artifact(incompleto)
