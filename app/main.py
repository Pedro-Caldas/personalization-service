"""Aplicacao FastAPI: serving de recomendacoes personalizadas.

A API carrega no startup o artefato pre-computado pelo job de batch e serve
recomendacoes com um lookup O(1) -- nunca carrega o modelo nem importa o
pipeline em tempo de request. Expoe:

- ``GET /health``               -- 503 ate o artefato carregar, 200 depois.
- ``GET /recommendations/{id}`` -- ranking personalizado ou fallback de cold start.
- ``GET /metrics``              -- metricas no formato Prometheus.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.artifact import load_artifact
from app.config import ARTIFACT_PATH
from app.observability import (
    ARTIFACT_LOAD_FAILURES,
    configure_logging,
    logger,
    observe_scores,
    record_cold_start,
    record_request,
    stage_timer,
)
from app.ranking import build_recommendations, resolve_limit
from app.schemas import (
    HealthResponse,
    RecommendationItem,
    RecommendationsResponse,
)

_ENDPOINT_RECS = "/recommendations/{user_id}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Carrega o artefato uma vez no startup.

    Falha ao carregar nao derruba o processo: o artefato fica ``None``, o
    ``/health`` passa a responder 503 e a falha e contabilizada -- deixando o
    load balancer parar de rotear trafego em vez de servir respostas quebradas.
    """
    configure_logging()
    app.state.artifact = None
    try:
        app.state.artifact = load_artifact(ARTIFACT_PATH)
        logger.info(
            "artefato carregado no startup",
            extra={
                "event": "artifact_loaded",
                "artifact_path": str(ARTIFACT_PATH),
                "model_version": app.state.artifact.model_version,
                "generated_at": app.state.artifact.generated_at,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- falha registrada, degrada via /health
        ARTIFACT_LOAD_FAILURES.inc()
        logger.error(
            "falha ao carregar o artefato no startup",
            extra={
                "event": "artifact_load_failed",
                "artifact_path": str(ARTIFACT_PATH),
                "error": str(exc),
            },
            exc_info=True,
        )
    yield


app = FastAPI(
    title="Personalization Service",
    description="Serving de recomendacoes de propensao de compra por usuario.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    """Prontidao do servico: 503 ate o artefato carregar, 200 depois.

    Semantica de status HTTP para que qualquer load balancer (com ou sem
    Kubernetes) saiba nao rotear trafego durante o startup.
    """
    artifact = app.state.artifact
    if artifact is None:
        response.status_code = 503
        return HealthResponse(status="loading", artifact_loaded=False)
    return HealthResponse(
        status="ok",
        artifact_loaded=True,
        model_version=artifact.model_version,
        generated_at=artifact.generated_at,
    )


@app.get("/metrics")
def metrics() -> Response:
    """Metricas no formato de exposicao do Prometheus."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/recommendations/{user_id}", response_model=RecommendationsResponse)
def get_recommendations(
    user_id: str,
    limit: int | None = Query(
        default=None,
        description="Numero de itens a retornar (default e teto definidos no serving).",
    ),
) -> RecommendationsResponse:
    """Ranking personalizado do usuario, ou fallback de cold start se desconhecido.

    Cold start nao e erro: retorna 200 com ``cold_start=true``. ``limit`` fora da
    faixa valida retorna 422; artefato ainda nao carregado retorna 503.
    """
    start = time.perf_counter()
    status_code = 200
    cold_start: bool | None = None
    try:
        artifact = app.state.artifact
        if artifact is None:
            status_code = 503
            raise HTTPException(
                status_code=503,
                detail="servico indisponivel: artefato ainda nao carregado",
            )

        try:
            limit_value = resolve_limit(limit)
        except ValueError as exc:
            status_code = 422
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        with stage_timer("lookup"):
            ctx = artifact.lookup(user_id)
        cold_start = ctx.cold_start

        with stage_timer("postprocess"):
            items, filter_applied = build_recommendations(
                ctx.ranking, ctx.purchased_product_ids, limit_value
            )

        record_cold_start(cold_start)
        observe_scores([item["score"] for item in items])

        with stage_timer("serialize"):
            body = RecommendationsResponse(
                user_id=user_id,
                cold_start=cold_start,
                purchase_filter_applied=filter_applied,
                model_version=artifact.model_version,
                generated_at=artifact.generated_at,
                recommendations=[RecommendationItem(**item) for item in items],
            )
        return body
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- vira 500; status registrado no finally
        status_code = 500
        raise
    finally:
        duration = time.perf_counter() - start
        record_request(_ENDPOINT_RECS, "GET", status_code, duration)
        logger.info(
            "requisicao de recomendacao concluida",
            extra={
                "event": "recommendation_request",
                "endpoint": _ENDPOINT_RECS,
                "user_id": user_id,
                "status_code": status_code,
                "cold_start": cold_start,
                "latency_ms": round(duration * 1000, 3),
            },
        )
