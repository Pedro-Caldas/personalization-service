# Planning — Personalization Service Case

## Propósito deste documento

Registro das decisões de arquitetura tomadas **antes** da implementação, com racional, trade-offs e evidências de cada escolha. Serve de base para (1) a implementação em uma sessão futura e (2) defender cada decisão na sabatina técnica.

**Princípio norteador**: todas as decisões buscam se aproximar do que seria feito em produção real (escala de milhões de dados/requisições), mesmo que o dataset do case seja pequeno (500 usuários, 60 produtos, 8000 eventos) — desde que a escolha seja defensável e explicável.

## Como usar este documento (leia isto primeiro — sessão de implementação)

Se você (Claude, numa sessão nova, sem memória desta conversa) está lendo isto: seu ponto de partida é este arquivo + o repositório `personalization_case/`, que já contém `data/`, `model/`, o `README.md` original do case, e este `PLANNING.md`.

- **Leia o `README.md` do case primeiro** (enunciado original), depois este documento **por completo**, antes de escrever qualquer código.
- **As seções 1–6 são decisões já fechadas**, com racional e evidência — não reabra essas discussões nem proponha alternativas a elas; implemente-as como estão descritas. Se notar uma inconsistência real entre seções, resolva a favor da versão mais recente/refinada (o documento foi escrito de forma incremental e alguns pontos foram corrigidos ao longo do processo — isso está sinalizado explicitamente onde ocorreu).
- **A seção 7** é o plano de implementação propriamente dito: estrutura de arquivos, esquema do artefato pré-computado, stack técnica e ordem recomendada de construção. É o seu guia prático.
- **Não confunda este documento com o `SOLUTION.md`** exigido pelo README do case. `PLANNING.md` é o histórico de raciocínio de design (verboso, com evidência e metodologia — serviu para preparar a defesa das decisões numa sabatina técnica). `SOLUTION.md` é o entregável final para os avaliadores: mais enxuto, cobrindo como rodar o projeto, decisões de arquitetura e trade-offs, o que seria feito diferente com mais tempo, e o que é logado/medido hoje. Escreva o `SOLUTION.md` ao final da implementação, resumindo (não copiando integralmente) as decisões já tomadas aqui.
- Para qualquer dúvida de escopo não coberta aqui: siga o mesmo princípio norteador usado durante todo o design — aproximar-se do que seria feito em produção real, de forma explicável, sem adicionar complexidade desproporcional ao ganho.

## Dados do case (fatos levantados)

- `events.csv`: 8000 linhas, 500 usuários únicos, 60 produtos únicos, período 2026-01-01 a 2026-05-30 (~5 meses).
- Distribuição de `event_type`: view=4382 (54.8%), click=1999 (25.0%), add_to_cart=1025 (12.8%), purchase=594 (7.4%) — funil clássico de conversão.
- `products.csv`: 60 produtos, 6 categorias (beleza, casa, eletronicos, esporte, livros, moda).
- 350 de 500 usuários (70%) têm pelo menos 1 evento de `purchase`.

## Status das decisões

- [x] 1. Pipeline de features (ingestão)
- [x] 2. Derivação de `user_affinity_match`
- [x] 3. Cold start
- [x] 4. Contrato da API / ranking
- [x] 5. Observabilidade
- [x] 6. Testes
- [x] 7. Plano de implementação (estrutura de arquivos, ordem de construção)

---

## 1. Pipeline de features (ingestão)

### Decisão

- **Separar o processo de preparo de dados (pipeline/job) do serving (API).**
  Um script/job (`prepare_features.py` ou equivalente) processa `events.csv` + `products.csv` uma única vez e materializa um artefato contendo:
  - Tabela de produtos com features estáticas (`price`, `avg_rating`, `popularity_score`, `category`).
  - Agregados por usuário: interações por produto e categoria de afinidade (ver decisão #2).
  - Ranking de recomendações **pré-computado** (batch scoring) por `user_id` conhecido.
  A API carrega esse artefato pronto no startup — não recomputa os CSVs brutos a cada boot.

- **Scoring em batch (pré-computado)** para usuários conhecidos: o endpoint de recomendação, para esses casos, é um lookup O(1) no ranking já calculado pelo job.

- **Scoring on-demand** reservado exclusivamente para o caso de cold start (usuário fora do batch).

### Racional / Trade-offs

- Reflete o padrão real de sistemas de propensão de compra em produção: scoring em batch (diário/horário, ex: via Airflow/cron) é o padrão dominante quando o sinal não depende de tempo real — e aqui não há stream de eventos, é um snapshot estático.
- Separar pipeline de serving permite testar cada parte isoladamente e agendar/escalar cada uma de forma independente, como em produção real (job de feature engineering vs. serviço de serving lendo de uma "feature store" simplificada).
- **Trade-off aceito**: usuários novos só entram no próximo ciclo de batch — sem atualização em tempo real. Aceitável porque não há stream de eventos no case; registrado como próximo passo (ver seção "o que faria diferente" no SOLUTION.md futuro).
- **Alternativa descartada**: computar tudo on-demand a cada request. Seria viável dado o tamanho do dataset (60 produtos, lookups baratos), mas não reflete o padrão de produção em escala nem demonstra separação de responsabilidades pipeline/serving.

### Nota sobre escala real (contexto para a sabatina)

Em produção real (milhões de usuários/produtos), o batch scoring não faz o cross-join ingênuo "todos os usuários × todo o catálogo" — isso explodiria em custo computacional e armazenamento. O padrão de mercado usa duas otimizações que **não são necessárias neste case** dado o tamanho do dataset, mas que valem registrar como consciência de escala:

1. **Duas etapas (candidate generation → ranking)**: uma etapa barata de retrieval reduz o catálogo a um conjunto pequeno de candidatos por usuário (ex: por afinidade de categoria, similaridade, popularidade) *antes* de rodar o modelo caro de ranking nesse subconjunto. Padrão descrito no paper de recomendação do YouTube (2016). Nesse case, com 60 produtos, o catálogo inteiro já é pequeno o suficiente para servir de "conjunto de candidatos" — a etapa de retrieval é desnecessária.
2. **Armazenamento por top-K, não pela tabela completa**: o artefato do batch guarda só as top-N recomendações por usuário (ex: top 20), não o score de todo produto para todo usuário — e normalmente em um key-value store rápido (Redis/DynamoDB) indexado por `user_id`, com TTL, não uma tabela relacional gigante.

A escolha de manter o processo simples (artefato único carregado em memória, sem candidate generation nem KV store distribuído) é deliberada e proporcional à escala do dataset do case — não desconhecimento do padrão de produção em escala maior.

### Refinamento (identificado na revisão final de fechamento do planejamento)

O job de batch pré-computa **dois tipos de ranking**, ambos com a mesma função de scoring reutilizável:

1. **Rankings por usuário conhecido** (um por `user_id` presente em `events.csv`), usando as features reais do histórico.
2. **Um único ranking "default" de cold start**, calculado uma única vez com features neutras (`interactions=0`, `user_affinity_match=0` para todos os produtos) — ver justificativa completa na seção 3.

Ambos ficam no mesmo artefato. Consequência importante: **a API nunca precisa carregar `model.pkl`/scaler em tempo de execução** — só o script de batch (`scripts/prepare_features.py`) depende de `scikit-learn`. Isso simplifica a API (menos dependências, boot mais rápido) e elimina a necessidade de qualquer "scoring on-demand" real dentro do processo da API — mesmo o cold start vira um lookup O(1), igual aos usuários conhecidos. Detalhes de implementação na seção 7.

---

## 2. Derivação de `user_affinity_match`

### Definição de referência (model card)

Categoria com maior número de interações do usuário (todos os tipos de evento contam igualmente 1), via join `events.csv` + `products.csv` por `product_id`.

### Decisão

Usar uma **definição ponderada por tipo de evento** (funil de intenção de compra), em vez de contagem simples:

| event_type | peso |
|---|---|
| view | 1 |
| click | 2 |
| add_to_cart | 3 |
| purchase | 5 |

A categoria de afinidade do usuário é a que acumula maior score ponderado (soma dos pesos por categoria, join com `products.csv`).

- **Critério de desempate**: ordem alfabética da categoria (determinístico). Não houve empates no dataset atual sob a contagem simples, mas o critério fica documentado para robustez em dados futuros.
- **Recência**: decaimento temporal (peso maior para eventos recentes) foi considerado e **descartado para a v1** — introduziria um hiperparâmetro (meia-vida do decaimento) sem forma de calibrar/validar sem um processo de backtest offline. Registrado como próximo passo.

### Racional / Evidência empírica

**Argumento teórico**: ponderar por tipo de evento reflete o padrão de sistemas de feedback implícito em produção (ex: Hu et al., 2008, "Collaborative Filtering for Implicit Feedback Datasets") — um `view` é sinal fraco de intenção, um `purchase` é confirmação máxima. Como `view` é a interação mais frequente (54.8% dos eventos), a contagem simples corre o risco de atribuir afinidade a categorias que o usuário só navegou casualmente, não onde ele de fato compra.

**Contraponto considerado**: o modelo foi treinado assumindo a definição de referência (contagem simples). Mudar o critério de cálculo da feature no serving introduz risco de **train-serving skew** — a distribuição da feature em produção pode divergir da distribuição vista no treino, degradando a calibração do modelo.

**Resolução via evidência empírica** (medida nos dados reais do case, 500 usuários / 8000 eventos):

| Comparação | Divergência |
|---|---|
| Contagem simples vs. ponderada por tipo de evento | **0,2%** (1 de 500 usuários) |
| Contagem simples vs. apenas `purchase` (entre os 350 usuários com histórico de compra) | **20,3%** (71 de 350 usuários) |
| Empates na contagem simples | 0% (0 de 500 usuários) |

**Conclusão**: a versão ponderada por tipo de evento é, na prática, quase idêntica à definição de referência (diverge em apenas 1 usuário de 500) — ganho teórico de alinhamento com o objetivo de negócio (propensão de *compra*), com risco de skew desprezível. Por isso foi adotada como definição final.

Uma versão "apenas `purchase`" foi cogitada (sinal mais puro de intenção), mas **descartada**: diverge em 20% dos casos com histórico de compra — risco real de skew sem forma de validar o impacto na calibração do modelo (sem acesso para retreinar/backtestar). Além disso, 30% dos usuários não têm nenhuma compra, exigindo de qualquer forma um fallback — o que recriaria um cold-start parcial dentro da lógica principal, aumentando complexidade sem benefício claro.

### Metodologia de validação (para referência/reprodução)

Script ad-hoc rodado na fase de design: join `events` + `products` por `product_id`, `groupby(user_id, category)` com três agregações (contagem simples, soma ponderada por `event_type`, contagem apenas de `purchase`), comparando a categoria "vencedora" (`argmax`, com desempate alfabético) entre as três definições. A lógica final será replicada dentro do pipeline de preparo de features (`prepare_features.py`).

---

## 3. Cold start

### Decisão

Usuários sem histórico em `events.csv` **continuam passando pelo mesmo modelo e mesmo caminho de código** do scoring on-demand (não há uma regra de negócio paralela / algoritmo alternativo). As duas features dependentes de histórico são neutralizadas:

- `interactions` = 0 (usuário nunca interagiu com nenhum produto)
- `user_affinity_match` = 0 para todos os produtos (não há categoria de afinidade conhecida)

O modelo ainda diferencia os produtos usando `price`, `avg_rating` e `popularity_score` — sinais que não dependem do histórico do usuário. O ranking final, portanto, tende a favorecer produtos bem avaliados e populares, sem personalização individual (o que é o comportamento esperado/correto para um usuário desconhecido).

### Racional / Trade-offs

- **Um único caminho de scoring** (usuário conhecido em batch vs. desconhecido on-demand usam a mesma função de featurização + mesmo modelo) é mais simples de operar, testar e explicar do que manter dois algoritmos de recomendação coexistindo (ex: modelo para uns, regra fixa de popularidade para outros). Menos superfície de código, menos comportamento a validar em produção.
- **Alternativa descartada**: regra de negócio explícita e separada (ex: ordenar direto por `popularity_score` ou `avg_rating`, sem passar pelo modelo). Mais simples de implementar isoladamente, mas cria dois sistemas de ranking distintos — dobra a lógica a manter e a testar, e complica a observabilidade (dois "motivos" diferentes para uma recomendação aparecer).
- Cold start é tratado como um **caso de feature values, não um caso de arquitetura separada** — reforça a decisão do item 1 (pipeline único, scoring on-demand já existe como caminho de exceção para usuários fora do batch).
- Requisito do README de logar "se houve fallback de cold start" (ver seção 5, Observabilidade) é atendido naturalmente: basta marcar quando `interactions`/`user_affinity_match` foram neutralizados por ausência de histórico.

### Refinamento (identificado na revisão final): pré-computação em vez de recomputação por request

Como cold start por definição significa "nenhum evento em `events.csv` para esse `user_id`", as features neutralizadas (`interactions=0`, `user_affinity_match=0` para todo produto) são **as mesmas para qualquer usuário desconhecido** — o resultado do scoring não depende de qual `user_id` específico está pedindo. Por isso, em vez de recomputar a cada request, o job de batch calcula esse ranking "default" **uma única vez** (ver seção 1, "Refinamento") e a API apenas faz lookup dele para qualquer `user_id` fora do batch — o mesmo caminho de código dos usuários conhecidos, só que apontando para uma entrada compartilhada em vez de uma por usuário.

Isso **não muda a arquitetura conceitual** decidida acima (um único caminho de featurização + scoring, reutilizado) — é uma otimização em cima dela, possível porque o conjunto de features atual (5 features do `model_card.json`) não tem nenhum sinal que dependa de algo além do histórico de eventos do usuário. **Premissa a documentar**: se no futuro o cold start passasse a considerar outro sinal que varia por usuário mesmo sem histórico de eventos (ex: atributos de cadastro, dispositivo, localização), essa pré-computação deixaria de ser válida e o caminho voltaria a precisar de cálculo genuinamente on-demand.

---

## 4. Contrato da API / ranking

### Decisões

**a) Quantidade de itens retornados**
Query param opcional `limit` (default 10, teto máximo de 50 para evitar payloads grandes). Padrão comum em APIs de recomendação — o cliente decide quantos itens cabem no seu uso, o serviço não impõe número fixo. Com catálogo de 60 produtos o teto é mais simbólico aqui, mas demonstra a prática correta.

**b) Filtro de produtos já comprados**
Produtos com evento `purchase` no histórico do usuário são removidos do conjunto de candidatos antes do ranking — aplicado como regra de negócio em cima do score do modelo (o score continua sendo a *base* do ranking, conforme exigido pelo README, mas não a única regra).
- Racional: sem esse filtro, o modelo tende a pontuar mais alto produtos com muitas interações do usuário — incluindo os já comprados — gerando recomendações do tipo "compre de novo o que você já tem", que é sinal de baixa qualidade para categorias não-consumíveis (maioria do catálogo: eletrônicos, livros, moda).
- Limitação documentada: a regra é aplicada uniformemente para todas as categorias porque `products.csv` não tem metadado de "consumível vs. durável". Em produção real essa regra variaria por categoria.

**Ajuste (identificado durante o planejamento de testes — eixo 6): fallback em cascata quando o filtro esvazia a lista.**
Se o usuário já comprou o catálogo inteiro (ou todos os produtos elegíveis), aplicar o filtro literalmente resultaria em uma lista de recomendações vazia — o pior resultado possível numa API de recomendação (tela em branco no produto). Regra ajustada: o filtro é aplicado normalmente; **se e somente se** o resultado ficar vazio, o filtro é desativado *para aquela resposta específica* e a API retorna o catálogo ranqueado completo (incluindo itens já comprados) em vez de nada. É o mesmo princípio de degradação graciosa já usado no cold start (item 3) — falhar suave em vez de falhar duro.
- Transparência: a resposta inclui o flag `purchase_filter_applied: bool`, no mesmo espírito do `cold_start: true` (item 4d) — o time de produto sabe se aquela resposta específica pode conter itens já comprados.
- Não é um "backfill parcial" (ex: completar até o `limit` misturando filtrados e não-filtrados) — a regra é binária (filtro ligado ou desligado por completo para a resposta) para manter a lógica simples e fácil de testar, evitando complexidade desproporcional ao ganho.

**c) Diversidade de categoria no top-N**
Fora do escopo da v1. Com 60 produtos em 6 categorias o ganho é marginal, e diferente da decisão #2 (afinidade), não há evidência empírica levantada que justifique a complexidade adicional agora. Registrado como próximo passo.

**d) Formato da resposta**
Por item: `product_id`, `score`, `rank`, `category`, `price` (evita forçar o consumidor da API a fazer outro lookup no catálogo — a API é consumida por times de produto, conforme o contexto do README).
No nível da resposta: `cold_start: bool` (indica se a recomendação é personalizada ou fallback), `purchase_filter_applied: bool` (indica se itens já comprados foram excluídos ou não — ver ajuste no item 4b), `model_version` e `generated_at` (rastreabilidade — essencial para depurar em produção "por que esse usuário recebeu essa recomendação").

**e) Health check**
Endpoint único `GET /health` (conforme pedido no README — "endpoint simples"). O payload expõe o estado dos componentes internos: processo up e artefato pré-computado carregado (o único componente relevante — ver refinamento na seção 1/3: a API não carrega `model.pkl`/scaler em tempo de execução, então não há um "modelo carregado" separado a verificar; o artefato já contém tudo que a API precisa, incluindo o ranking de cold start).

Nota de design (ajustada após conversa sobre a stack real do banco, que não usa Kubernetes): o conceito de separar "processo vivo" de "processo pronto para servir" **não é exclusivo do Kubernetes** — é um padrão genérico usado por qualquer load balancer ou ferramenta de deploy (F5, nginx, HAProxy, ALB, scripts de blue-green) para decidir se deve rotear tráfego a uma instância. Por isso, em vez de dois endpoints com nomenclatura k8s-específica (`/health/live`, `/health/ready`), a decisão final é: **um único `/health`, mas com status HTTP semântico** — retorna `503` durante o startup (enquanto o artefato pré-computado ainda não terminou de carregar) e `200` quando pronto. Qualquer load balancer, com ou sem Kubernetes, sabe interpretar `503` como "não rotear tráfego ainda". Isso preserva o valor prático do padrão sem assumir uma infraestrutura específica que não existe no ambiente real do avaliador.

**f) Contrato de erro**
Cold start **não é um erro** — é fluxo esperado, retorna `200` com `cold_start: true` no payload. Erros reais (falha ao carregar modelo/artefato, exceção interna) retornam `500`. Não há validação forte de formato de `user_id`: qualquer string vira usuário conhecido (batch) ou cold start (on-demand) — não existe "usuário inválido" no domínio do problema.

---

## 5. Observabilidade

### Contexto de pesquisa

O modelo é de propensão de compra, e a "métrica de qualidade real" (o usuário de fato comprou o que foi recomendado?) só existiria com um horizonte de dias/semanas depois da recomendação — o case não tem loop de feedback pós-recomendação. Isso é o problema conhecido em MLOps como **"delayed ground truth"** (comum também em modelos de crédito e fraude). A literatura (Evidently AI, Datadog — ver fontes) converge em: quando não há ground truth em tempo real, o melhor proxy disponível é monitorar a **distribuição das saídas do modelo (prediction drift)** — a lógica é que scores servidos com distribuição estável em relação à referência do treino/pipeline sugerem que o modelo continua se comportando como esperado; uma mudança abrupta é sinal de alerta mesmo sem saber o resultado real.

Quatro camadas de observabilidade identificadas na pesquisa, e aplicabilidade ao case:

| Camada | O que é | Aplica aqui? |
|---|---|---|
| Service health | Latência, taxa de erro, throughput (RED method) | Sim — exigido pelo README |
| Data/pipeline health | Sucesso do job de batch, validação de schema de entrada | Sim, barato e alto valor |
| Model output monitoring | Distribuição dos scores servidos (proxy de drift sem ground truth) | Sim — melhor substituto possível dado que não há feedback real |
| Model quality (ground truth) | Precision/recall/AUC reais | Não aplicável neste case (sem loop de feedback) |

### Decisão: implementar Nível 1, documentar Nível 2 como próximo passo

**Nível 0 (mínimo exigido pelo README)** — base:
- Logs estruturados (JSON) no endpoint principal: `user_id`, latência (ms), `cold_start` (bool), endpoint, status_code, timestamp.
- Métricas via `/metrics` (formato Prometheus): contagem de requisições, taxa de erro, latência (p50/p95).

**Nível 1 (implementado — baixo custo, alto valor, embasado na pesquisa)** — adicionado ao Nível 0:
- **Taxa de cold start como métrica agregada** (counter/gauge), não só campo de log — natural já que o dado já existe por request.
- **Histograma de distribuição dos scores servidos** — é o proxy de *prediction drift* recomendado pela pesquisa como item de maior valor na ausência de ground truth em tempo real.
- **Latência quebrada por etapa** (lookup do ranking pré-computado no artefato vs. pós-processamento de regras de negócio — filtro de já-comprados, `limit` — vs. serialização da resposta) — barato de instrumentar já que o código naturalmente separa essas etapas; ajuda a diagnosticar gargalo. Nota: como a API não roda inferência do modelo em tempo real (ver seção 1, "Refinamento"), não há uma etapa de "inferência" no caminho de request — a duração de execução do job de batch (que essa sim roda o modelo) é medida separadamente, como métrica do pipeline, não do request HTTP.
- **Contagem de falhas de carregamento do pipeline/artefato** como métrica/log de erro dedicado — coerente com a decisão do `/health` retornando `503` até o artefato carregar (item 4e).

**Nível 2 (documentado, não implementado — decisão consciente de escopo)**:
- **Feature drift real** entre execuções do pipeline (comparar distribuição das features de entrada ao longo do tempo): tecnicamente correto, mas o case só fornece *um* snapshot estático — não há "antes e depois" real para comparar. Implementar agora seria simulado, sem dado real para validar. A pesquisa também aponta isso como não essencial para serviços de baixo tráfego.
- **Model quality com ground truth real** (precision@K, recall@K): exigiria rastrear se a recomendação gerou compra de fato. Arquitetura futura: gravar um `recommendation_id` junto com cada recomendação servida, e cruzar depois com eventos de compra reais (quando/se chegarem) para calcular métricas de acerto com atraso.

### Cuidado técnico (cardinalidade)

`user_id` **nunca** deve ser usado como label de métrica Prometheus — causa explosão de cardinalidade (problema bem documentado na literatura de operação de Prometheus). `user_id` fica exclusivamente nos logs estruturados; métricas agregadas usam labels de baixa cardinalidade (endpoint, status_code, cold_start=true/false).

### Fontes de pesquisa

- [Model monitoring for ML in production: a comprehensive guide (Evidently AI)](https://www.evidentlyai.com/ml-in-production/model-monitoring)
- [Machine learning model monitoring: Best practices (Datadog)](https://www.datadoghq.com/blog/ml-model-monitoring-in-production-best-practices/)
- [Monitoring Regression Models Without Ground-Truth (MLOps Community)](https://mlops.community/blog/monitoring-regression-models-without-ground-truth)
- [How to Manage Metric Cardinality in Prometheus](https://oneuptime.com/blog/post/2026-01-25-prometheus-metric-cardinality/view)
- [Monitoring ML systems in production: which metrics should you track? (Evidently AI)](https://www.evidentlyai.com/blog/ml-monitoring-metrics)

---

## 6. Testes

### Decisões

**a) Framework**
`pytest` + `TestClient` do FastAPI para toda a suíte, inclusive o teste de integração. O README aceita explicitamente essa opção como suficiente ("ex: TestClient do FastAPI, ou um container real"). Container Docker real fica reservado para verificação manual (`docker-compose up`), não para a suíte automatizada — evita complexidade de infra (docker-in-docker em CI) sem ganho real de cobertura.

**b) Dados de teste: fixtures pequenas para unitários, dado real para o teste de integração**
- **Unitários**: fixtures pequenas e determinísticas (`tests/fixtures/`) — CSVs reduzidos criados à mão (3-4 usuários, 5-6 produtos) cobrindo os casos de interesse (afinidade clara, histórico de compra, usuário sem histórico). Resultado esperado calculável manualmente, teste legível e determinístico.
- **Integração**: usa o `model.pkl` e os CSVs **reais** do case, através da execução real do pipeline (`prepare_features.py`) no setup do teste. Racional: o propósito do teste de integração (conforme o README, "garantir que as peças realmente funcionam juntas") só é cumprido testando contra o artefato real gerado pelo pipeline real — é onde se pegam problemas de costura entre pipeline e modelo (ex: ordem de `feature_cols` desalinhada, scaler incompatível, `InconsistentVersionWarning` do scikit-learn — ver seção 7) que um fixture sintético nunca reproduziria.

**c) Cobertura dos testes unitários**
- Derivação de `user_affinity_match` (definição ponderada por tipo de evento + critério de desempate alfabético — item 2).
- Montagem do vetor de features (produto com interações conhecidas, produto com zero interações do usuário).
- Neutralização de features no cold start (`interactions=0`, `user_affinity_match=0` — item 3).
- Filtro de produtos já comprados, **incluindo o caso extremo do catálogo inteiro já comprado**: o resultado esperado é o catálogo completo ranqueado normalmente com `purchase_filter_applied=False`, não uma lista vazia (ver ajuste no item 4b — lacuna identificada durante este planejamento de testes).
- Comportamento do `limit`: default, customizado, acima do teto, valor inválido (0 ou negativo).

**d) Cobertura do teste de integração**
Fluxo completo, sem mockar nenhuma camada interna: o setup do teste roda o `prepare_features.py` real contra os dados/modelo reais do case (gerando um artefato de verdade), depois sobe a aplicação via `TestClient` apontando para esse artefato. Isso testa pipeline + serving juntos, não só a API isolada. Três chamadas HTTP reais: `GET /health`, `GET /recommendations/{user_id}` para um usuário conhecido do dataset real, e para um `user_id` inexistente (cold start). Valida status code, schema completo da resposta, e que os flags (`cold_start`, `purchase_filter_applied`) batem com o esperado em cada caso.

**e) Filosofia de mock: não mockar o modelo/scaler, nem nos unitários**
`LogisticRegression.predict_proba` custa microssegundos — mockar o sklearn adicionaria complexidade (fixture de mock, manutenção do contrato do mock) sem ganho real de velocidade ou isolamento nessa escala. "Unitário", aqui, significa isolar a lógica de negócio pura (cálculo de features, regras de filtro/desempate) da camada HTTP — não isolar do sklearn. Mock só se justificaria se o modelo fosse caro de rodar ou dependesse de rede/serviço externo, o que não é o caso.

**f) Estrutura de pastas (detalhado no item 7)**
`tests/unit/` (features, cold start, regras de ranking), `tests/integration/` (fluxo HTTP completo), `tests/fixtures/` (CSVs pequenos usados nos unitários).

---

## 7. Plano de implementação

### Requisitos técnicos (do README, consolidados aqui para referência rápida)

- Python **>= 3.12** (exigido pelo case).
- **`scikit-learn` deve ser pinado em `1.8.0`** (não a versão mais recente disponível, e não qualquer `>=`). Verificado empiricamente: `model.pkl` foi serializado com sklearn 1.8.0; carregar com 1.5.2 (testado durante o planejamento) gera `InconsistentVersionWarning` em `model` e `scaler`, com risco documentado pelo próprio sklearn de "resultados inválidos". Isso só afeta o script de batch (`scripts/prepare_features.py`), já que a API não carrega o modelo (ver seção 1/3).
- Framework de API: **FastAPI** (implícito desde a seção 6 pelo uso de `TestClient`, agora explícito). Racional: integração nativa com Pydantic para os schemas de request/resposta, `TestClient` embutido para os testes, geração automática de documentação OpenAPI (valor extra para os times de produto que vão consumir a API, conforme o contexto do README).
- Servidor ASGI: `uvicorn`.
- Containerização: Docker (fortemente recomendado pelo README) — `Dockerfile` baseado em `python:3.12-slim`, com o `CMD`/`ENTRYPOINT` rodando `prepare_features.py` antes de subir o `uvicorn` (ou um `docker-compose` com dois passos).
- Gerenciamento de dependências: `requirements.txt` simples (evita a complexidade adicional de poetry/uv para um projeto deste tamanho — decisão consciente, documentável como trade-off no SOLUTION.md).

### Estrutura de arquivos proposta

```
personalization_case/
├── data/                          # dado do case (não modificar)
├── model/                         # modelo do case (não modificar)
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app: endpoints, startup (carrega artefato), health check
│   ├── config.py                  # constantes: WEIGHTS do funil (item 2), DEFAULT_LIMIT, MAX_LIMIT, path do artefato
│   ├── schemas.py                 # modelos Pydantic de request/resposta (item 4d)
│   ├── ranking.py                 # pós-processamento em tempo de request: filtro de já-comprados + fallback (item 4b), limit, rank
│   ├── artifact.py                # schema do artefato pré-computado + load
│   └── observability.py           # logging estruturado (JSON) + métricas Prometheus (item 5)
├── pipeline/
│   ├── __init__.py
│   ├── features.py                # funções puras: derivação de affinity ponderada (item 2), montagem do vetor de features
│   └── scoring.py                 # carrega model.pkl/scaler, roda o modelo sobre o catálogo inteiro para um dado vetor de features de usuário
├── scripts/
│   └── prepare_features.py        # orquestra pipeline/: lê CSVs, gera artefato com rankings de usuários conhecidos + ranking default de cold start
├── tests/
│   ├── unit/
│   │   ├── test_features.py       # derivação de affinity, montagem de features (usa pipeline/features.py + fixtures)
│   │   ├── test_ranking.py        # filtro de já-comprados + fallback de catálogo vazio + limit (usa app/ranking.py)
│   │   └── test_cold_start.py     # feature vector neutro sempre igual, independente do user_id
│   ├── integration/
│   │   └── test_api.py            # roda prepare_features.py real, sobe a app via TestClient, testa os 3 fluxos (item 6d)
│   └── fixtures/
│       ├── events_fixture.csv     # pequeno, determinístico (item 6b)
│       └── products_fixture.csv
├── artifacts/                     # gerado por prepare_features.py — gitignored, não versionar
│   └── features_artifact.json
├── Dockerfile
├── requirements.txt
├── .gitignore                     # inclui artifacts/, __pycache__, etc. (já existe um .gitignore no repo — revisar/estender)
├── README.md                      # enunciado original do case — não sobrescrever
├── PLANNING.md                    # este documento
└── SOLUTION.md                    # entregável final (escrever ao final — ver "Como usar este documento")
```

`pipeline/` e `app/` são pacotes separados deliberadamente: `pipeline/` depende de `pandas` e `scikit-learn` (só usado pelo script de batch), `app/` depende só de `fastapi`, `pydantic` e `prometheus-client` — reforça em código a separação arquitetural decidida na seção 1 (pipeline vs. serving), e é uma dependência unidirecional: `app/` nunca importa de `pipeline/`.

### Esquema do artefato pré-computado (`artifacts/features_artifact.json`)

```json
{
  "model_version": "purchase_propensity_v1@1.0.0",
  "generated_at": "<timestamp ISO 8601 da execução do batch>",
  "known_users": {
    "<user_id>": {
      "affinity_category": "<categoria, para contexto de log/explicabilidade>",
      "purchased_product_ids": ["<product_id>", "..."],
      "ranking": [
        {"product_id": "...", "score": 0.0, "category": "...", "price": 0.0}
      ]
    }
  },
  "cold_start_ranking": [
    {"product_id": "...", "score": 0.0, "category": "...", "price": 0.0}
  ]
}
```

Notas de design do artefato:
- `ranking` guarda o catálogo **completo** (todos os produtos elegíveis), já ordenado por score — não só top-K. Racional: com 60 produtos isso é trivial em tamanho, e guardar a lista completa permite que o filtro de já-comprados (item 4b) e o `limit` sejam aplicados em tempo de request sem precisar re-rodar o modelo. Em escala real (milhões de produtos), esse formato mudaria para top-K + reranking restrito (ver nota de escala na seção 1).
- `purchased_product_ids` é o que alimenta o filtro do item 4b diretamente — não é necessário guardar a contagem de interações por produto no artefato final (era só um intermediário do cálculo de features, descartável após o scoring).
- `affinity_category` é mantido mesmo não sendo mais necessário para scoring, porque é útil para contexto em log estruturado (explicabilidade: "por que esse produto foi recomendado" pode citar a categoria de afinidade).
- Formato **JSON**, não pickle: decisão consciente — pickle executaria código arbitrário ao carregar (risco de segurança desnecessário para um artefato interno) e JSON é legível/inspecionável a olho nu, útil para debugar. Dado o tamanho pequeno, não há motivo de performance para preferir um formato binário (parquet, etc.).

### Ordem recomendada de construção

Construir de baixo para cima (funções puras → orquestração → API → integração → infra), validando cada camada antes de depender dela na próxima:

1. Esqueleto do projeto: pastas, `requirements.txt` (com `scikit-learn==1.8.0` fixado), `.gitignore` estendido.
2. `pipeline/features.py`: derivação de `user_affinity_match` (item 2) + montagem do vetor de features. Funções puras, sem I/O.
3. `tests/fixtures/` + `tests/unit/test_features.py`: validar `features.py` contra os fixtures pequenos antes de seguir.
4. `pipeline/scoring.py`: carregar `model.pkl`, rodar o modelo sobre o catálogo inteiro dado um perfil de usuário (função reutilizável para usuário conhecido e para o perfil neutro de cold start — item 1/3).
5. `scripts/prepare_features.py`: orquestra 2+4 contra os dados reais (`data/`, `model/`), gera `artifacts/features_artifact.json` com os rankings de usuários conhecidos + o ranking default de cold start. Rodar manualmente e inspecionar o JSON gerado antes de seguir.
6. `app/artifact.py` + `app/config.py`: carregar o artefato gerado no passo 5.
7. `app/ranking.py`: filtro de já-comprados + fallback de catálogo vazio (item 4b) + aplicação de `limit` + atribuição de rank. `tests/unit/test_ranking.py` e `test_cold_start.py` aqui (incluindo o caso extremo corrigido na seção 6c).
8. `app/schemas.py`: modelos Pydantic de resposta (item 4d, com `cold_start`, `purchase_filter_applied`, `model_version`, `generated_at`).
9. `app/observability.py`: logging estruturado JSON + métricas Prometheus (item 5, Nível 1).
10. `app/main.py`: endpoints `GET /health` (200/503 conforme item 4e) e `GET /recommendations/{user_id}` (lookup em `known_users` ou fallback para `cold_start_ranking`), com logging/métricas plugados.
11. `tests/integration/test_api.py`: roda o pipeline real + sobe a app real via `TestClient` (item 6d).
12. `Dockerfile`: build multi-step lógico (rodar `prepare_features.py` antes do `uvicorn` subir) — validar com `docker build` + `docker run` manual.
13. `SOLUTION.md`: escrever por último, resumindo as decisões deste documento no formato exigido pelo README (como rodar, decisões e trade-offs, o que faria diferente, o que loga/mede hoje).

### O que documentar no `SOLUTION.md` como "próximos passos" (consolidado das seções anteriores)

- Recência/decaimento temporal na derivação de afinidade (item 2) — não implementado por falta de forma de calibrar sem backtest.
- Diversidade de categoria no ranking (item 4c) — fora de escopo, ganho marginal dado o tamanho do catálogo.
- Feature drift real entre execuções do pipeline (item 5, Nível 2) — não há múltiplas execuções reais neste case para comparar.
- Model quality com ground truth real / precision@K com atraso (item 5, Nível 2) — exigiria rastrear `recommendation_id` e cruzar com compras futuras.
- Separação do container de serving (sem `pandas`/`scikit-learn` em runtime) do container/step do pipeline de batch, via build multi-stage do Docker — mencionado aqui pela primeira vez: dado que `app/` já não depende dessas bibliotecas (ver seção 1/3, refinamento), essa separação é uma otimização natural de tamanho de imagem que não foi implementada no case por simplicidade, mas decorre diretamente da arquitetura escolhida.

---

**Planejamento fechado.** Todas as sete seções foram decididas, documentadas com racional/evidência, e revisadas para consistência interna nesta releitura final.
