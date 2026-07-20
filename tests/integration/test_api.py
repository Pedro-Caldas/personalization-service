"""Teste de integracao ponta a ponta: pipeline real + serving real.

Nao mocka nenhuma camada interna. O setup roda o job de batch de verdade
(``python -m scripts.prepare_features``, que carrega ``model.pkl`` e processa os
CSVs reais do case) para materializar um artefato real, e sobe a aplicacao via
``TestClient`` apontando para ele. Isso exercita a costura entre pipeline e
serving -- ordem de ``feature_cols``, compatibilidade do scaler, formato do
artefato -- que fixtures sinteticos nao reproduziriam.

Fluxos cobertos: ``GET /health`` (pronto), ``GET /recommendations/{user_id}``
para um usuario conhecido do dataset e para um usuario inexistente (cold start),
e ``GET /metrics`` refletindo a atividade real das requisicoes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]

_ITEM_FIELDS = {"product_id", "score", "rank", "category", "price"}
_DEFAULT_LIMIT = 10  # default do serving; a resposta nao deve exceder o teto padrao


@pytest.fixture(scope="module")
def artifact_path(tmp_path_factory) -> Path:
    """Gera um artefato real executando o job de batch como em producao."""
    path = tmp_path_factory.mktemp("artifact") / "features_artifact.json"
    subprocess.run(
        [sys.executable, "-m", "scripts.prepare_features", "--output", str(path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    assert path.exists(), "job de batch nao gerou o artefato"
    return path


@pytest.fixture(scope="module")
def artifact_data(artifact_path: Path) -> dict:
    """Conteudo do artefato real -- fonte da verdade para checar as respostas."""
    return json.loads(artifact_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def client(artifact_path: Path):
    """Sobe a app real apontando o serving para o artefato gerado.

    O lifespan de ``app.main`` le o global ``ARTIFACT_PATH`` no momento em que o
    ``TestClient`` entra no contexto; sobrescreve-lo aqui direciona a carga sem
    recarregar modulos. O ``with`` dispara o startup (carga do artefato).
    """
    import app.main as main

    original = main.ARTIFACT_PATH
    main.ARTIFACT_PATH = artifact_path
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.ARTIFACT_PATH = original


def _assert_valid_item(item: dict) -> None:
    """Valida o schema de um item de recomendacao."""
    assert set(item) == _ITEM_FIELDS
    assert isinstance(item["product_id"], str)
    assert isinstance(item["score"], float)
    assert isinstance(item["rank"], int)
    assert isinstance(item["category"], str)
    assert isinstance(item["price"], float)
    assert 0.0 <= item["score"] <= 1.0


def _pick_known_user_with_purchases(artifact_data: dict) -> str:
    """Um usuario conhecido com historico de compra (para exercer o filtro)."""
    for user_id, data in artifact_data["known_users"].items():
        if data["purchased_product_ids"]:
            return user_id
    pytest.skip("nenhum usuario com compras no dataset real")


def test_health_pronto_apos_carga(client: TestClient, artifact_data: dict):
    """/health responde 200 e expoe a versao do modelo depois do startup."""
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["artifact_loaded"] is True
    assert body["model_version"] == artifact_data["model_version"]
    assert body["generated_at"] == artifact_data["generated_at"]


def test_recomendacao_usuario_conhecido(client: TestClient, artifact_data: dict):
    """Usuario conhecido: ranking personalizado, sem itens ja comprados."""
    user_id = _pick_known_user_with_purchases(artifact_data)
    purchased = set(artifact_data["known_users"][user_id]["purchased_product_ids"])

    resp = client.get(f"/recommendations/{user_id}")

    assert resp.status_code == 200
    body = resp.json()

    # Metadados da resposta.
    assert body["user_id"] == user_id
    assert body["cold_start"] is False
    assert isinstance(body["purchase_filter_applied"], bool)
    assert body["model_version"] == artifact_data["model_version"]
    assert body["generated_at"] == artifact_data["generated_at"]

    # Lista de recomendacoes: nao vazia, dentro do teto, schema e rank corretos.
    recs = body["recommendations"]
    assert 0 < len(recs) <= _DEFAULT_LIMIT
    for item in recs:
        _assert_valid_item(item)
    assert [item["rank"] for item in recs] == list(range(1, len(recs) + 1))

    # Score e a base do ranking: ordem decrescente.
    scores = [item["score"] for item in recs]
    assert scores == sorted(scores, reverse=True)

    # Com o filtro ativo, nenhum item recomendado foi comprado pelo usuario.
    if body["purchase_filter_applied"]:
        returned_ids = {item["product_id"] for item in recs}
        assert returned_ids.isdisjoint(purchased)


def test_cold_start_usuario_inexistente(client: TestClient, artifact_data: dict):
    """Usuario fora do historico: 200 com fallback de cold start (nao e erro)."""
    resp = client.get("/recommendations/usuario_que_nao_existe_xyz")

    assert resp.status_code == 200
    body = resp.json()

    assert body["user_id"] == "usuario_que_nao_existe_xyz"
    assert body["cold_start"] is True
    # Sem historico -> nada a filtrar, mas o filtro esta ativo.
    assert body["purchase_filter_applied"] is True
    assert body["model_version"] == artifact_data["model_version"]

    recs = body["recommendations"]
    assert 0 < len(recs) <= _DEFAULT_LIMIT
    for item in recs:
        _assert_valid_item(item)

    # O ranking de cold start e o mesmo para qualquer usuario desconhecido.
    outro = client.get("/recommendations/outro_desconhecido_abc")
    assert outro.status_code == 200
    assert outro.json()["recommendations"] == recs


def test_metrics_reflete_requisicoes_reais(client: TestClient):
    """/metrics expoe Prometheus e contabiliza a atividade das requisicoes."""
    # Gera atividade conhecida: um conhecido e um cold start.
    client.get("/recommendations/u_0000")
    client.get("/recommendations/nao_existe_para_metrica")

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text

    # RED por requisicao + taxa de cold start (ambos os rotulos presentes).
    assert "recommendation_requests_total" in body
    assert 'recommendation_cold_start_total{cold_start="true"}' in body
    assert 'recommendation_cold_start_total{cold_start="false"}' in body
    # Proxy de drift e latencia por etapa foram instrumentados.
    assert "recommendation_served_score_count" in body
    assert 'recommendation_stage_duration_seconds_count{stage="lookup"}' in body
