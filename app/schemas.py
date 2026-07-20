"""Modelos Pydantic de request/resposta da API."""

from __future__ import annotations

from pydantic import BaseModel


class RecommendationItem(BaseModel):
    product_id: str
    score: float
    rank: int
    category: str
    price: float


class RecommendationsResponse(BaseModel):
    user_id: str
    cold_start: bool
    purchase_filter_applied: bool
    model_version: str
    generated_at: str
    recommendations: list[RecommendationItem]


class HealthResponse(BaseModel):
    status: str  # "ok" quando pronto, "loading" durante o startup
    artifact_loaded: bool
    model_version: str | None = None
    generated_at: str | None = None
