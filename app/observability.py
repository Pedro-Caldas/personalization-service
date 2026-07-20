"""Observabilidade do serving: logs estruturados (JSON) + metricas Prometheus.

Duas frentes, prontas para serem consumidas por uma stack real depois:

- **Logs estruturados** (uma linha JSON por evento em stdout): cada request do
  endpoint principal registra ``user_id``, latencia, ``cold_start``, status e
  endpoint. ``user_id`` fica *apenas* aqui -- nunca vira label de metrica.
- **Metricas Prometheus** expostas em ``/metrics``: contagem/erro/latencia por
  requisicao (RED), taxa de cold start, distribuicao dos scores servidos (proxy
  de prediction drift na ausencia de ground truth), latencia por etapa do
  request e contagem de falhas de carregamento do artefato.

Cuidado de cardinalidade: labels de metrica sao todos de baixa cardinalidade
(endpoint, status_code, cold_start, stage). ``user_id`` jamais entra como label.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from prometheus_client import Counter, Histogram

# --- Logs estruturados (JSON) --------------------------------------------

# Atributos padrao de um LogRecord: tudo que nao estiver aqui e tratado como
# campo extra do evento e serializado no JSON.
_RESERVED_LOG_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime"}


class JsonLogFormatter(logging.Formatter):
    """Serializa cada LogRecord como uma unica linha JSON.

    Campos passados via ``logger.info(msg, extra={...})`` sao promovidos para o
    nivel superior do objeto, para ficarem indexaveis pela stack de logs.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Instala o formatter JSON no root logger, escrevendo em stdout.

    Idempotente: substitui handlers existentes para evitar linhas duplicadas em
    reinicios/reload da aplicacao.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


logger = logging.getLogger("personalization")


# --- Metricas Prometheus --------------------------------------------------

# RED por requisicao: contagem (por status_code -> taxa de erro derivavel) e
# latencia (histograma -> p50/p95 no Prometheus).
REQUESTS_TOTAL = Counter(
    "recommendation_requests_total",
    "Total de requisicoes ao endpoint de recomendacao.",
    ["endpoint", "method", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "recommendation_request_duration_seconds",
    "Latencia fim-a-fim das requisicoes de recomendacao.",
    ["endpoint"],
)

# Taxa de cold start: counter dedicado (label true/false), agregavel sem tocar
# nos logs. cold_start rate = increments[true] / (true + false).
COLD_START_TOTAL = Counter(
    "recommendation_cold_start_total",
    "Requisicoes atendidas por caminho personalizado vs. fallback de cold start.",
    ["cold_start"],
)

# Distribuicao dos scores servidos: proxy de prediction drift quando nao ha
# ground truth em tempo real. Buckets cobrem a faixa de probabilidade [0, 1].
SERVED_SCORES = Histogram(
    "recommendation_served_score",
    "Distribuicao dos scores dos produtos efetivamente servidos.",
    buckets=(
        0.01, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4,
        0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0,
    ),
)

# Latencia por etapa do request (lookup no artefato / pos-processamento de
# regras / serializacao) -- ajuda a localizar gargalo. Buckets finos porque as
# etapas sao sub-milissegundo (a API nao roda o modelo em tempo de request).
STAGE_LATENCY = Histogram(
    "recommendation_stage_duration_seconds",
    "Latencia por etapa do processamento de uma recomendacao.",
    ["stage"],
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1),
)

# Falhas de carregamento do artefato no startup -- coerente com o /health que
# retorna 503 enquanto o artefato nao carrega.
ARTIFACT_LOAD_FAILURES = Counter(
    "artifact_load_failures_total",
    "Total de falhas ao carregar o artefato pre-computado.",
)


def record_request(
    endpoint: str, method: str, status_code: int, duration_seconds: float
) -> None:
    """Registra contagem (com status) e latencia de uma requisicao concluida."""
    REQUESTS_TOTAL.labels(
        endpoint=endpoint, method=method, status_code=str(status_code)
    ).inc()
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration_seconds)


def record_cold_start(cold_start: bool) -> None:
    """Incrementa o counter de cold start (label ``true``/``false``)."""
    COLD_START_TOTAL.labels(cold_start=str(cold_start).lower()).inc()


def observe_scores(scores: list[float]) -> None:
    """Registra no histograma cada score efetivamente servido."""
    for score in scores:
        SERVED_SCORES.observe(score)


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    """Mede a duracao de uma etapa e a registra em ``STAGE_LATENCY``."""
    start = time.perf_counter()
    try:
        yield
    finally:
        STAGE_LATENCY.labels(stage=stage).observe(time.perf_counter() - start)
