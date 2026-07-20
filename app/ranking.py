"""Pos-processamento de recomendacoes em tempo de request.

Regras de negocio aplicadas em cima do score do modelo (que continua sendo a
base do ranking): resolucao de ``limit``, filtro de ja-comprados com fallback
gracioso, e atribuicao de ``rank``.
"""

from __future__ import annotations

from app.config import DEFAULT_LIMIT, MAX_LIMIT


def resolve_limit(limit: int | None) -> int:
    """Normaliza o ``limit`` pedido.

    - ``None`` -> ``DEFAULT_LIMIT``.
    - acima de ``MAX_LIMIT`` -> limitado ao teto (protege o payload).
    - menor que 1 -> ``ValueError`` (entrada invalida; o chamador traduz p/ 422).
    """
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise ValueError(f"limit deve ser >= 1 (recebido: {limit})")
    return min(limit, MAX_LIMIT)


def apply_purchase_filter(
    ranking: list[dict], purchased_product_ids: list[str]
) -> tuple[list[dict], bool]:
    """Remove produtos ja comprados do ranking.

    Fallback em cascata: se o filtro esvaziar a lista (usuario ja comprou todo o
    catalogo elegivel), ele e desativado para esta resposta e o catalogo completo
    ranqueado e devolvido -- degradacao graciosa em vez de tela em branco.

    Retorna ``(ranking_selecionado, purchase_filter_applied)``. O flag indica se
    a resposta esta livre de itens ja comprados (``True``) ou pode conte-los
    porque o filtro foi desativado no fallback (``False``).
    """
    purchased = set(purchased_product_ids)
    filtered = [item for item in ranking if item["product_id"] not in purchased]
    if filtered:
        return filtered, True
    return ranking, False


def build_recommendations(
    ranking: list[dict], purchased_product_ids: list[str], limit: int
) -> tuple[list[dict], bool]:
    """Aplica filtro de ja-comprados, corta em ``limit`` e atribui ``rank``.

    Retorna ``(items, purchase_filter_applied)``; cada item ganha ``rank`` 1..N.
    """
    selected, filter_applied = apply_purchase_filter(ranking, purchased_product_ids)
    items = [
        {**item, "rank": rank}
        for rank, item in enumerate(selected[:limit], start=1)
    ]
    return items, filter_applied
