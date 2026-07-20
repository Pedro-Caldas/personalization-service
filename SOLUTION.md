# SOLUTION — Personalization Service

Microserviço HTTP que serve recomendações de produtos personalizadas por usuário,
a partir de um modelo de propensão de compra já treinado (`sklearn.LogisticRegression`).

## Visão geral da arquitetura

O sistema é dividido em duas responsabilidades independentes:

```
  data/*.csv + model/model.pkl
              │
              ▼
   ┌─────────────────────┐        artifacts/                ┌──────────────────┐
   │  pipeline / batch    │  ───►  features_artifact.json ──►│  API / serving   │
   │  (roda o modelo)     │        (rankings prontos)        │  (lookup O(1))   │
   └─────────────────────┘                                   └──────────────────┘
     pandas + scikit-learn                                     fastapi + prometheus
```

1. **Pipeline (batch)** — `scripts/prepare_features.py` lê os CSVs, calcula as
   features, roda o modelo sobre o catálogo inteiro para cada usuário e
   **materializa um artefato JSON** com os rankings já pré-computados. Roda uma
   vez, antes da API subir.
2. **Serving (API)** — a aplicação FastAPI carrega esse artefato no startup e
   responde cada request com um **lookup O(1)**. A API **não carrega o modelo**
   nem importa o pipeline: só depende do JSON.

Essa separação reflete o padrão de produção de sistemas de propensão de compra
(scoring em batch, serving lê de uma "feature store" simplificada) e permite
testar, escalar e implantar cada parte de forma independente.

## Como rodar

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Gera o artefato de recomendações (job de batch)
python -m scripts.prepare_features        # escreve artifacts/features_artifact.json

# 2. Sobe a API
uvicorn app.main:app --reload             # http://localhost:8000
```

- Documentação interativa (OpenAPI): `http://localhost:8000/docs`
- O caminho do artefato é configurável via variável de ambiente `ARTIFACT_PATH`.

### Docker

```bash
docker build -t personalization-service .
docker run -p 8000:8000 personalization-service
```

O container roda o job de batch no startup (materializa o artefato) e só então
sobe a API — enquanto o artefato carrega, `/health` responde `503`.

### Testes

```bash
pytest                    # suíte completa (unitários + integração)
pytest tests/unit         # só unitários
pytest tests/integration  # só integração (roda o pipeline real)
```

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/health` | Prontidão: `503` até o artefato carregar, `200` depois. |
| `GET` | `/recommendations/{user_id}` | Ranking personalizado (ou fallback de cold start). Aceita `?limit=` (default 10, teto 50). |
| `GET` | `/metrics` | Métricas no formato Prometheus. |
| `GET` | `/docs` | Documentação OpenAPI automática. |

Exemplo de resposta de `/recommendations/{user_id}`:

```json
{
  "user_id": "u_0000",
  "cold_start": false,
  "purchase_filter_applied": true,
  "model_version": "purchase_propensity_v1@1.0.0",
  "generated_at": "2026-07-18T21:50:35.666781+00:00",
  "recommendations": [
    {"product_id": "p_046", "score": 0.127965, "rank": 1, "category": "casa", "price": 589.09}
  ]
}
```

## Decisões de arquitetura e trade-offs

### Scoring em batch pré-computado, não on-demand

O ranking de cada usuário conhecido é calculado uma vez pelo job de batch e
servido por lookup. Como o dataset é um snapshot estático (sem stream de
eventos), não há ganho em recomputar a cada request — e o padrão de batch é o
dominante em produção quando o sinal não depende de tempo real.
**Trade-off**: usuários novos só entram no próximo ciclo de batch. Aceitável aqui
(sem stream de eventos); registrado nos próximos passos.

### A API não carrega o modelo

Como todos os rankings (inclusive o de cold start) são pré-computados, a API
precisa apenas do artefato JSON — nunca do `model.pkl`, do scaler ou do
`scikit-learn`. Isso simplifica o serving (menos dependências, boot mais rápido)
e reduz sua superfície. `pipeline/` e `app/` são pacotes separados justamente
para deixar essa fronteira explícita: `app/` nunca importa de `pipeline/`.

### Feature `user_affinity_match`: ponderada pelo funil de conversão

A categoria de afinidade do usuário é a de maior score **ponderado por tipo de
evento** (`view`=1, `click`=2, `add_to_cart`=3, `purchase`=5), não a de simples
contagem de interações. A intuição é que um `view` é sinal fraco de intenção e um
`purchase` é confirmação máxima — coerente com o objetivo de *propensão de compra*.
Desempate por ordem alfabética da categoria (determinístico).

Medido nos dados reais, essa definição diverge da contagem simples (referência do
model card) em apenas **1 de 500 usuários (0,2%)** — ou seja, ganho de alinhamento
com o objetivo de negócio com risco de *train-serving skew* desprezível. Uma
versão "apenas purchase" foi descartada: divergiria em ~20% dos casos, risco real
de skew sem forma de validar o impacto na calibração do modelo.

### Cold start: valores de feature neutros, mesmo caminho de código

Usuário sem histórico não tem um algoritmo paralelo — passa pelo **mesmo modelo**,
com as features dependentes de histórico neutralizadas (`interactions=0`,
`user_affinity_match=0`). O modelo ainda diferencia produtos por `price`,
`avg_rating` e `popularity_score`, então o ranking tende a favorecer produtos bem
avaliados e populares — o comportamento esperado para um usuário desconhecido.
Como esse vetor neutro é idêntico para qualquer usuário novo, o ranking de cold
start é pré-computado **uma vez** e servido por lookup, igual aos usuários
conhecidos. Um único caminho de scoring é mais simples de operar, testar e
explicar do que dois sistemas de ranking coexistindo.

### Contrato de ranking

- **`limit`**: query param opcional (default 10, teto 50 para proteger o payload).
- **Filtro de já-comprados**: produtos com `purchase` no histórico são removidos
  antes do ranking — evita recomendar "compre de novo o que você já tem". O score
  do modelo continua sendo a base do ranking; o filtro é uma regra por cima dele.
- **Fallback gracioso**: se o filtro esvaziaria a lista (usuário comprou o catálogo
  elegível inteiro), ele é desativado *só para aquela resposta* e o catálogo
  completo ranqueado é devolvido — falhar suave em vez de tela em branco. O flag
  `purchase_filter_applied` na resposta sinaliza qual dos dois ocorreu.
- **Transparência**: a resposta inclui `cold_start`, `purchase_filter_applied`,
  `model_version` e `generated_at` — rastreabilidade para depurar em produção "por
  que esse usuário recebeu essa recomendação".

### Health check com status HTTP semântico

Um único `GET /health` que retorna `503` durante o startup (enquanto o artefato
carrega ou se ele falhou ao carregar) e `200` quando pronto. Qualquer load
balancer — com ou sem Kubernetes — sabe interpretar `503` como "não roteie tráfego
ainda", sem precisar de endpoints com nomenclatura específica de orquestrador.

### Artefato em JSON, não pickle

O artefato é JSON legível/inspecionável, não pickle: pickle executaria código
arbitrário ao carregar (risco de segurança desnecessário para um artefato interno)
e, dado o tamanho pequeno, não há motivo de performance para um formato binário.
Ele guarda o **catálogo completo ranqueado** por usuário (não só top-K), o que
permite aplicar o filtro de já-comprados e o `limit` em tempo de request sem
re-rodar o modelo.

### Stack e dependências

FastAPI (Pydantic para schemas, `TestClient` embutido, OpenAPI automático),
`uvicorn` como servidor ASGI, `prometheus-client` para métricas. `scikit-learn` é
**pinado em `1.8.0`**, a versão exata com que o `model.pkl` foi serializado —
carregar com outra versão gera `InconsistentVersionWarning` com risco de resultados
inválidos. Gestão de dependências via `requirements.txt` simples (sem poetry/uv),
proporcional ao tamanho do projeto.

## Observabilidade

Logs e métricas são desenhados para serem consumidos por uma stack real depois
(Grafana/Datadog/Loki), sem integração acoplada agora.

### O que é logado hoje

Uma linha **JSON por requisição** no endpoint principal, em stdout, com:
`user_id`, `latency_ms`, `cold_start`, `endpoint`, `status_code` e `timestamp`.
O startup também loga o carregamento do artefato (ou a falha).

### O que é medido hoje (`/metrics`, Prometheus)

- **RED por requisição**: contagem (`recommendation_requests_total`, com
  `status_code` → taxa de erro derivável) e latência (`recommendation_request_duration_seconds`
  → p50/p95).
- **Taxa de cold start** (`recommendation_cold_start_total`, label `true`/`false`).
- **Distribuição dos scores servidos** (`recommendation_served_score`): na ausência
  de ground truth em tempo real (o "comprou de fato?" só se sabe dias depois), a
  distribuição das saídas do modelo é o melhor proxy disponível de *prediction drift*.
- **Latência por etapa** (`recommendation_stage_duration_seconds`: `lookup` /
  `postprocess` / `serialize`) — ajuda a localizar gargalo.
- **Falhas de carregamento do artefato** (`artifact_load_failures_total`), coerente
  com o `/health` retornando `503`.

### Cuidado de cardinalidade

`user_id` aparece **apenas nos logs**, nunca como label de métrica Prometheus —
usar identificadores de alta cardinalidade como label causa explosão de séries.
Os labels de métrica são todos de baixa cardinalidade (`endpoint`, `status_code`,
`cold_start`, `stage`).

## Testes

- **Unitários** (`tests/unit/`): derivação de afinidade ponderada e desempate,
  montagem do vetor de features, neutralização no cold start, filtro de já-comprados
  incluindo o caso extremo do catálogo inteiro comprado, e comportamento do `limit`
  (default, customizado, acima do teto, inválido). Usam fixtures pequenas e
  determinísticas.
- **Integração** (`tests/integration/test_api.py`): roda o `prepare_features.py`
  **real** contra os dados/modelo reais do case (gerando um artefato de verdade) e
  sobe a app via `TestClient`, sem mockar nenhuma camada interna. Valida os fluxos
  de `/health`, usuário conhecido, cold start e `/metrics` — é onde se pegam
  problemas de costura entre pipeline e serving (ordem de `feature_cols`,
  compatibilidade do scaler, formato do artefato).

Não se mocka o modelo/scaler nem nos unitários: `predict_proba` custa
microssegundos, e mockar o sklearn adicionaria complexidade sem ganho de isolamento
nessa escala.

## O que eu faria diferente / próximos passos

- **Recência na afinidade**: aplicar decaimento temporal (eventos recentes pesam
  mais). Não implementado por introduzir um hiperparâmetro (meia-vida) sem forma de
  calibrar sem backtest offline.
- **Diversidade de categoria no top-N**: hoje o ranking pode concentrar produtos de
  uma só categoria. Ganho marginal dado o catálogo de 60 produtos; registrado como
  refinamento.
- **Atualização de usuários novos em tempo real**: hoje eles só entram no próximo
  ciclo de batch. Com um stream de eventos, um caminho de scoring on-demand cobriria
  a lacuna.
- **Model quality com ground truth**: gravar um `recommendation_id` por recomendação
  servida e cruzar depois com compras reais para calcular precision@K / recall@K com
  atraso — a métrica de qualidade que hoje não existe por não haver loop de feedback.
- **Feature drift real** entre execuções do pipeline: exigiria múltiplos snapshots ao
  longo do tempo, que o case não fornece.
- **Container de serving slim**: como `app/` não depende de `pandas`/`scikit-learn`,
  um build Docker multi-stage separaria a imagem de serving (leve) da etapa de batch.
- **Alertas e tracing distribuído**: alertas sobre as métricas já expostas (ex.:
  drift dos scores, taxa de erro) e trace IDs propagados ponta a ponta.

## Estrutura do projeto

```
app/                 # serving (FastAPI): endpoints, artefato, ranking, schemas, observabilidade
  ├── main.py            # endpoints /health, /recommendations, /metrics
  ├── artifact.py        # carga e lookup do artefato pré-computado
  ├── ranking.py         # filtro de já-comprados + fallback, limit, rank
  ├── schemas.py         # modelos Pydantic de request/resposta
  ├── observability.py   # logs JSON + métricas Prometheus
  └── config.py          # constantes e ARTIFACT_PATH
pipeline/            # batch: funções puras de feature + scoring (pandas + sklearn)
  ├── features.py        # derivação de afinidade + montagem do vetor de features
  └── scoring.py         # carrega o modelo e ranqueia o catálogo
scripts/
  └── prepare_features.py  # orquestra o job de batch e gera o artefato
tests/               # unit/ + integration/ + fixtures/
Dockerfile           # imagem única: roda o batch no startup e sobe a API
```
