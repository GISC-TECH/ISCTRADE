# Exchanges: adaptadores, viabilidade e roadmap de integração

> [!CAUTION]
> **Não é parecer jurídico nem recomendação de investimento.** As informações regulatórias abaixo foram
> levantadas em **21/10/2026**, em plena transição do novo marco do Banco Central (prazo de 30/10/2026), e mudam
> rápido. Confirme a situação de cada exchange e a sua própria situação (residência fiscal, tipo de conta,
> tributação) antes de operar. Derivativos alavancados podem causar **perda total** do capital.

## Sumário
1. [Como o ISCTRADE fala com exchanges hoje (adaptador)](#1-como-o-isctrade-fala-com-exchanges-hoje-adaptador)
2. [Contexto regulatório no Brasil (out/2026)](#2-contexto-regulatório-no-brasil-out2026)
3. [Tabela comparativa](#3-tabela-comparativa)
4. [Análise por exchange](#4-análise-por-exchange)
5. [B3: mini-índice e mini-dólar via MetaTrader 5 ou Nelogica](#5-b3-mini-índice-e-mini-dólar-via-metatrader-5-ou-nelogica)
6. [CCXT / ccxt.pro: usar ou não?](#6-ccxt--ccxtpro-usar-ou-não)
7. [Recomendação e ordem de prioridade](#7-recomendação-e-ordem-de-prioridade)
8. [Como adicionar uma nova exchange](#8-como-adicionar-uma-nova-exchange)

---

## 1. Como o ISCTRADE fala com exchanges hoje (adaptador)

Todo serviço que conversa com uma exchange (execução, reconciliador, equity, dashboard, kill switch, scanner,
backtest, streams, trading manual) depende da interface **`ExchangeAdapter`**
(`backend/app/exchanges/base.py`), nunca de um cliente concreto.

| Peça | Arquivo | O que faz |
|---|---|---|
| Interface | `app/exchanges/base.py` | `ExchangeAdapter` (ABC, async): dados de mercado REST, klines (`get_klines`, `get_klines_range`), filtros/precisão (`get_symbol_filters` → `SymbolFilters`), tickers 24h, mark price/funding (`get_premium_index_all`, `get_funding_rate_history`), conta/saldo/posições, alavancagem e modo de margem, ordens com `client_order_id` (`place_order`, `cancel_order`, `get_order`, `get_open_orders`, `cancel_all_orders`), PnL realizado, user-data stream (`user_data_stream`) e streams de mercado (`market_stream`). Também define `ExchangeAPIError`, `UnsupportedFeatureError`, `ExchangeCapabilities` e os **códigos de erro canônicos** (`ERR_ORDER_NOT_FOUND = -2013`, `ERR_CANCEL_REJECTED = -2011`, `ERR_NO_CHANGE = -4046`, `ERR_INVALID_SYMBOL = -1121`). |
| Registro/fábrica | `app/exchanges/registry.py` | `adapter_for_account(account)` (assinado, credenciais da conta), `public_adapter(testnet)` (dados públicos), `create_adapter(id, ...)`, `available_exchanges()`, `register_exchange(cls)`. Adaptadores `experimental=True` são recusados se `EXPERIMENTAL_EXCHANGES_ENABLED=false` (padrão). |
| Binance USDT-M | `app/services/binance_client.py` (+ `binance_ws.py`, `binance_http.py`), alias em `app/exchanges/binance_usdm.py` | Implementação de produção (`exchange_id = "binance_usdm"`). Comportamento idêntico ao anterior à refatoração. |
| Bybit V5 linear | `app/exchanges/bybit.py`, `app/exchanges/bybit_ws.py` | **EXPERIMENTAL** (`bybit_linear`), desligado por padrão: REST completo traduzido para o formato canônico; WebSockets público e privado traduzidos para eventos canônicos, ligados só com `BYBIT_WS_ENABLED=true`. Só para desenvolvimento em Testnet. |
| Conta | `accounts.exchange` (migração `0006`) | Id do adaptador por conta (padrão `binance_usdm`). Validado na criação; `GET /api/v1/accounts/exchanges` lista os habilitados. |

**Formato canônico.** Para manter a refatoração incremental, os payloads trocados entre adaptadores e serviços
são os da **Binance USDT-M** (klines em lista, `positionRisk`, `ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE`, status
`NEW/PARTIALLY_FILLED/FILLED/CANCELED/...`). Cada novo adaptador **traduz** o formato nativo da sua exchange para
esse formato (o docstring de `base.py` lista os campos usados). A Binance não traduz nada.

**Pontos que ainda assumem a exchange padrão** (a resolver quando uma 2ª exchange for para produção):
- feed de mark price/tickers compartilhado (`workers/streams.py` → `MarketStream`, `workers/market_feed.py`) e o
  cache de preços (`services/prices.py`), que são indexados só por `testnet`, não por exchange;
- filtros do trading manual e do paper (`manual_trading._default_filters`) e o scanner/backtest usam
  `public_adapter()` da exchange padrão;
- símbolos são no formato Binance (`BTCUSDT`); cada adaptador converte (OKX: `BTC-USDT-SWAP`, Gate: `BTC_USDT`,
  KuCoin: `XBTUSDTM`, Hyperliquid: `BTC`);
- `new_client_order_id()` gera `btp-<28 hex>` (32 caracteres). OKX não aceita `-` em `clOrdId`; Hyperliquid
  exige `cloid` hexadecimal de 128 bits — o adaptador deve derivar um id determinístico e reversível;
- o frontend ainda não oferece a escolha de exchange no cadastro de conta.

---

## 2. Contexto regulatório no Brasil (out/2026)

- **Resoluções BCB 519, 520 e 521** (nov/2025, vigentes desde **02/02/2026**) criaram as **SPSAV/PSAV**
  (prestadoras de serviços de ativos virtuais) com exigência de autorização, sede e diretoria no Brasil, capital,
  segregação patrimonial e PLD/CFT. Quem já operava tem até **30/10/2026** para pedir autorização; exchanges
  **estrangeiras** precisam transferir clientes brasileiros para uma entidade autorizada ou **encerrar** a
  atividade no país. A Res. 521 trata operações com ativos virtuais no mercado de câmbio (reporte desde 04/05/2026).
- **Derivativos (futuros/perpétuos) são valores mobiliários** (Lei 6.385/1976) e a sua oferta a residentes é
  matéria da **CVM**. A CVM já tratou a oferta de derivativos cripto por exchanges estrangeiras como irregular
  (a Binance fechou acordo de R$ 9,6 milhões em 2024).
- Efeito prático em out/2026:
  - **Binance**: a Binance Brasil (entidades locais) **não oferece derivativos a residentes no Brasil**; segundo a
    própria Binance, quem quiser operá-los "por iniciativa própria" usaria uma **conta internacional separada**
    da entidade de Abu Dhabi (ADGM), **fora** da supervisão da CVM e do BCB. Mudanças a partir de 27–29/10/2026.
  - **Bybit**: migrou residentes para a Bybit Brasil e **encerrou derivativos/margem para residentes a partir de
    21/09/2026**; pediu autorização de PSAV ao BC em 20/10/2026.
  - **OKX**: opera via entidade brasileira (OKX Serviços Digitais Ltda.) com produtos limitados para usuários
    locais; noticiado pedido de licença ao BC (out/2026).
  - **Bitget**: sem confirmação pública de pedido; BingX, Gate, KuCoin e MEXC: sem entidade brasileira conhecida —
    risco alto de restrição/encerramento para residentes.
  - **DEX (Hyperliquid, dYdX)**: não têm entidade nem KYC; o Brasil não está na lista de países bloqueados dessas
    interfaces, mas a situação regulatória é **cinzenta** (oferta de derivativo sem registro) e a obrigação de
    declarar à Receita continua.
  - **Exchanges brasileiras** (Mercado Bitcoin, Foxbit, NovaDAX, Binance BR): **somente spot**, reguladas pelo BC.
  - **B3** (WIN/WDO): totalmente regulado (CVM/B3), via corretora — o único caminho 100 % regular para
    **alavancagem** de um residente.

**Consequência para o ISCTRADE:** a exchange padrão (Binance USDT-M) deixa de ser oferecida a residentes
brasileiros pela entidade local. Para uso pessoal, avalie com assessoria jurídica se a conta é de residente, se a
operação via entidade estrangeira é aceitável para você e como declarar. O código não decide isso por você.

---

## 3. Tabela comparativa

Esforço estimado **a partir do adaptador atual**, para paridade com a Binance (REST + WS + reconciliação + testes
com HTTP mockado), por 1 dev experiente: **P** ≈ 2–4 dias, **M** ≈ 1–2 semanas, **G** ≈ 3–5 semanas,
**GG** > 6 semanas (novo domínio no produto).

| Exchange | Perpétuos | API / WebSocket | Testnet / demo | Brasil (residente, out/2026) | Limites (ordens) | Esforço |
|---|---|---|---|---|---|---|
| **Binance USDT-M** ✅ | Sim (USDT/USDC-M) | Excelente; listenKey, docs maduras | Testnet ✔ | **Derivativos não ofertados a residentes**; só via conta internacional ADGM | 2400 peso/min por IP; 1200 ordens/min e 300/10 s por conta | **feito** |
| **Bybit V5** 🧪 | Sim (linear/inverse) | Excelente; V5 unificado, WS privado com auth | Testnet ✔ + Demo trading ✔ | Derivativos **encerrados para residentes** (21/09/2026); PSAV pedida | ~600 req/5 s por IP; ~10–20 req/s por UID em `order/create` | **P–M** (esqueleto REST pronto) |
| **OKX V5** | Sim (SWAP) | Excelente; WS também para ordens | Demo trading ✔ (`x-simulated-trading: 1`) | Entidade BR com produtos limitados; licença pedida | 60 req/2 s por UID+instrumento | **M** |
| **Bitget** | Sim (USDT-M/USDC-M/coin-M) | Boa (V2/UTA) | Demo trading ✔ (`paptrading: 1`) | Sem pedido confirmado | 10 req/s por UID | **M** |
| **BingX** | Sim (perpetual swap) | Razoável; docs menos precisas | Demo VST (USDT virtual) — verificar host/API | Sem entidade BR conhecida | 10 req/s (ordinário, desde out/2025) | **M** |
| **Gate.io** | Sim (USDT/BTC-settled) | Boa (API v4); quantidade em **contratos** | Testnet de futuros ✔ | Sem entidade BR conhecida | ~100 req/s (criar+alterar) | **M** |
| **KuCoin Futures** | Sim | Boa; quantidade em **lotes**; WS com token | **Sandbox descontinuado (2023)** ✘ | Sem entidade BR conhecida | Pool de pesos por UID/30 s (varia por VIP) | **M–G** |
| **MEXC** | Sim | API de futuros com ordens só desde **31/03/2026** (KYC); histórico de restrições a bots | ✘ | Sem entidade BR conhecida | 4 req/2 s em `order/create` | **M–G** (risco de bloqueio de conta) |
| **Hyperliquid** | Sim (DEX on-chain, USDC) | Muito boa; WS rico; assinatura EIP-712 (SDK Python oficial) | Testnet ✔ | Sem KYC/geobloqueio p/ BR; regulatório cinzento | 1200 peso/min por IP; limite por endereço (10 000 iniciais + 1 por USDC negociado) | **M–G** |
| **dYdX v4** | Sim (DEX, cadeia Cosmos) | Indexer REST/WS bom; ordens via transação (gRPC/nó) | Testnet ✔ | Sem geobloqueio p/ BR; regulatório cinzento | Indexer 100 req/10 s por IP; ordens limitadas por bloco | **G** |
| **Binance BR (spot)** | ✘ (só spot) | Mesma API spot da Binance | Testnet spot ✔ | Regulada (entidades locais) | Limites da API spot | **G** (exige modo spot) |
| **Mercado Bitcoin** | ✘ (só spot BRL) | API v4 (OAuth2 client credentials) + WS público | Não identificado | Brasileira, regulada | Modestos (por endpoint) | **G** (modo spot) + P |
| **Foxbit** | ✘ (só spot BRL) | REST v3 + WS v3 (OpenAPI) | Não identificado | Brasileira, regulada | Modestos | **G** (modo spot) + P |
| **NovaDAX** | ✘ (só spot BRL) | REST HMAC + WS Socket.IO | Não identificado | Brasileira | Modestos | **G** (modo spot) + P |
| **B3 via MT5 / ProfitDLL** | Futuros (WIN, WDO) — não perpétuos | MT5 Python (Windows) / DLL Nelogica | Conta demo MT5 ✔ / simulador Profit | **Totalmente regulado** | Do terminal/corretora | **GG** |

Legenda: ✅ produção · 🧪 experimental no repositório.

---

## 4. Análise por exchange

### Bybit (V5) — esqueleto já no repositório
- **Prós:** API V5 unificada (`category=linear`), quantidades em moeda-base como a Binance, `orderLinkId` (36 chars)
  compatível com nosso `client_order_id`, ordens condicionais com `triggerPrice/triggerDirection` (mapeadas a
  partir de `STOP_MARKET`/`TAKE_PROFIT_MARKET`), Testnet e Demo trading.
- **Contras:** conta unificada (UTA) muda margem isolada/cruzada **por conta** (não por símbolo);
  `order/create` responde só com ack (status chega por WS ou consulta); erros "não modificado" (110043/110026)
  precisam ser tratados como idempotentes (já feito).
- **WebSockets (experimental, `BYBIT_WS_ENABLED=false` por padrão)** — `app/exchanges/bybit_ws.py`, reaproveita o
  laço de reconexão do `BinanceStream` (backoff, idade máxima, *idle timeout*, alerta de desconexão, métricas) e
  envia o `{"op":"ping"}` exigido a cada 20 s:
  - **público** (`/v5/public/linear`): `<sym>@markPrice[@1s]` e `<sym>@miniTicker` → tópico `tickers.<SYM>`
    (snapshot + *deltas* mesclados por símbolo) → `markPriceUpdate` / `24hrMiniTicker`; `<sym>@kline_<i>` →
    `kline.<i>.<SYM>` → evento `kline`. Assinaturas em lotes de 10. Streams de todo o mercado (`!miniTicker@arr`,
    `!markPrice@arr`) não existem na Bybit e são recusados (`UnsupportedFeatureError`);
  - **privado** (`/v5/private`): `auth` (HMAC-SHA256 de `"GET/realtime" + expires`; falha → reconecta com backoff)
    e assinatura de `order.linear`, `execution.linear`, `position.linear`, `wallet` → `ORDER_TRADE_UPDATE`
    (ordem: `x` = status; execução `execType=Trade`: `x=TRADE`, `t`, `l`, `L`, `n`, `rp`=`execPnl`, `z`) e
    `ACCOUNT_UPDATE` (`P` com `pa` assinado e `ps=BOTH` só para `positionIdx=0`; `B` com saldo USDT). Execuções de
    funding/ADL são ignoradas no stream (ficam para a reconciliação REST). Nenhuma ordem é enviada pelo socket.
  - Testado apenas com socket simulado (`tests/test_bybit_ws.py`); **não** foi validado contra a Testnet real.
    Com a flag desligada o `StreamSupervisor` pula a conta (REST a cada `RECONCILE_SECONDS`).
- **Falta:** validação na Testnet real, feed de preços por exchange (o feed compartilhado ainda é o da exchange
  padrão), funding no PnL realizado; hedge mode.

### OKX (V5)
- Excelente API e Demo trading. Cuidado: `sz` em **contratos** (`ctVal`), `instId` `BTC-USDT-SWAP`, ordens
  algorítmicas (stop) em endpoint separado (`/trade/order-algo`), `clOrdId` só alfanumérico (≤ 32). Requer
  `contract_size` nos filtros (ver §8). Boa 2ª/3ª opção, sobretudo se a OKX Brasil liberar derivativos.

### Bitget
- API V2/UTA boa, Demo trading via header. Modos de produto (`USDT-FUTURES`), TP/SL como planos. Sem sinal
  regulatório no Brasil — prioridade média-baixa.

### BingX
- Popular no varejo brasileiro, conta demo VST. Documentação e consistência de payloads inferiores; limites
  ampliados em 2025. Prioridade baixa.

### Gate.io
- API v4 sólida, **testnet de futuros real** (bom para CI manual). Quantidade em contratos (`quanto_multiplier`),
  `settle` no path. Prioridade média-baixa.

### KuCoin Futures
- Sem sandbox desde 2023 (testes só com HTTP mockado + capital mínimo real), lotes (`multiplier`), símbolos
  próprios (`XBTUSDTM`), WS exige token via REST. Prioridade baixa.

### MEXC
- Taxas baixas atraem bots, mas a própria MEXC **congela contas** por automação não autorizada e só abriu ordens
  de futuros via API em 31/03/2026 (KYC, taxa de API diferente, 4 req/2 s). Risco operacional alto — não
  recomendado.

### Hyperliquid
- DEX de perpétuos com livro on-chain, latência baixa, WS completo (`userEvents`, `orderUpdates`, `l2Book`,
  `candle`), Testnet e SDK oficial. Diferenças: autenticação por **carteira** (chave privada de uma *API wallet*
  dedicada, nunca a carteira principal), assinatura EIP-712, ativos por índice do `meta`, `cloid` hex 128 bits,
  margem em USDC. Boa alternativa técnica para quem não pode usar CEX; avalie o risco regulatório e de custódia
  (bridge, smart contract).

### dYdX v4
- Indexer (REST/WS) ótimo para dados; ordens são **transações** na cadeia (short-term vs stateful, subcontas,
  `goodTilBlock`), exigindo cliente gRPC/nó. Maior esforço entre as DEX. Prioridade baixa.

### Brasileiras (Binance BR spot, Mercado Bitcoin, Foxbit, NovaDAX)
- **Não têm perpétuos.** Integrá-las exige um **modo spot** no produto: sem alavancagem, sem posição vendida,
  saldo por ativo em BRL, ordens sem `reduceOnly`, outras regras no `RiskManager` e nas estratégias (só *long*).
  Depois disso, cada adaptador spot é pequeno (P). Úteis para quem quer 100 % regulado no BC, sem derivativos.

---

## 5. B3: mini-índice e mini-dólar via MetaTrader 5 ou Nelogica

**Viável, mas é outro produto.** WIN (mini-índice) e WDO (mini-dólar) são futuros com vencimento (rolagem),
pregão com horário, leilões, margem de garantia da corretora, tick fixo (WIN 5 pontos; WDO 0,5 ponto), custos de
corretagem/emolumentos e tributação própria (day trade 20 %, IRRF). Nada disso existe no domínio atual
(perpétuos 24/7 com funding).

| Caminho | Como funciona | Prós | Contras |
|---|---|---|---|
| **MetaTrader 5 + pacote Python `MetaTrader5`** | Terminal MT5 da corretora (XP, Clear, Genial, Modal etc. — confirme se a sua oferece MT5 para B3) logado; o pacote Python conversa com o terminal local (`copy_rates_*`, `order_send`, `positions_get`). | Gratuito na maioria das corretoras, conta **demo**, API simples, símbolos contínuos (`WIN$N`, `WDO$N`). | **Só Windows** (o pacote fala com o terminal via IPC); em Linux/Docker exige VM Windows ou Wine (frágil). Sem WebSocket nativo: polling ou `copy_ticks_*`. Um terminal por conta. |
| **Nelogica ProfitDLL** | DLL (Windows) com market data B3 em tempo real e roteamento (`DLLInitializeLogin`, `SendOrder`), há wrappers Python comunitários. | Dados B3 de qualidade, roteamento para muitas corretoras, callbacks em tempo real. | **Licença paga**, Windows, integração via ctypes/callbacks, contrato com a Nelogica. |

**Arquitetura sugerida:** um **"bridge" Windows** separado (serviço pequeno em Python ao lado do MT5/ProfitDLL)
expondo uma API interna autenticada (HTTP + WebSocket) com os mesmos verbos do `ExchangeAdapter`; no ISCTRADE,
um `B3BridgeAdapter` fala com esse bridge. Antes disso, o produto precisa de: calendário de pregão, vencimentos e
rolagem, contratos inteiros (`step_size=1`), valor do ponto (WIN R$ 0,20/ponto; WDO R$ 10,00/ponto) no cálculo de
risco, margem informada pela corretora e PnL em BRL. **Esforço: GG** (6–10+ semanas). Recomendado só como
trilha separada, depois do paper trading aprovado no cripto.

---

## 6. CCXT / ccxt.pro: usar ou não?

[CCXT](https://github.com/ccxt/ccxt) (MIT) unifica 100+ exchanges; o **ccxt.pro** (WebSockets: `watchTicker`,
`watchOHLCV`, `watchOrders`, `watchPositions`) hoje faz parte do pacote gratuito.

**Prós**
- Cobertura enorme e rápida de prototipar (Bybit, OKX, Bitget, BingX, Gate, KuCoin, MEXC, Hyperliquid, dYdX,
  Mercado Bitcoin, Foxbit, NovaDAX…), assinatura e *rate limiter* prontos, reconexão/resubscrição no ccxt.pro.
- Ótimo para **dados públicos** (scanner, backtest, klines históricos) e para validar a tradução de um adaptador
  nativo (comparar respostas).

**Contras (latência e correção)**
- **Latência/overhead:** camada extra de parsing e normalização em Python (CPU por mensagem), *throttle*
  conservador; benchmarks comunitários mostram dezenas a ~100 ms a mais em REST em alguns casos. Irrelevante para
  candles de 1 h, relevante para scalping/grids apertados.
- **Correção:** a unificação esconde detalhes que importam para dinheiro real: `amount` em contratos vs moeda-base
  (`contractSize`), modos de precisão (`TICK_SIZE`/`DECIMAL_PLACES`), parâmetros específicos (`reduceOnly`,
  `closePosition`, `triggerDirection`, `positionIdx`) via `params` não padronizados, mapeamento de erros genérico
  (difícil distinguir "ordem não existe" de "rejeitada"), campos `None` quando a exchange não informa, e mudanças
  de comportamento entre versões (lançamentos muito frequentes) — exige *pin* de versão e testes de regressão.
- Idempotência por `clientOrderId` e reconciliação continuam sendo responsabilidade nossa.

**Posição recomendada:** **híbrida.** Adaptadores **nativos** para as venues de execução (Binance, depois Bybit,
OKX, Hyperliquid), porque o caminho de ordem e o reconciliador precisam de semântica exata. Um **`CcxtAdapter`
experimental** (somente leitura no início: klines, tickers, funding, filtros) pode servir à cauda longa de
exchanges para scanner/backtest. Execução real via CCXT só com testes específicos por exchange e conta mínima.

---

## 7. Recomendação e ordem de prioridade

1. **Binance USDT-M** — manter como padrão técnico (pronta e testada). ⚠️ Para residentes no Brasil, derivativos
   só via entidade estrangeira (ADGM) a partir do fim de out/2026: decisão jurídica do usuário.
2. **Bybit V5** — completar o esqueleto (WS público/privado → eventos canônicos; reconciliador; testes) e rodar em
   **Testnet**. Melhor relação esforço/qualidade e menor distância da Binance. Mesmo caveat regulatório para
   residentes (derivativos encerrados na Bybit Brasil).
3. **OKX V5** — introduzir `contract_size` nos filtros (beneficia Gate/KuCoin/MEXC depois). Acompanhar se a OKX
   Brasil passa a ofertar derivativos de forma regular.
4. **Hyperliquid** — alternativa on-chain sem KYC, com Testnet e boa API; exige gestão segura de *API wallet*.
5. **`CcxtAdapter` somente leitura** — dados de mercado para scanner/backtest em outras venues.
6. **Modo spot + Mercado Bitcoin / Foxbit / Binance BR** — caminho 100 % regulado no BC, sem alavancagem.
7. **B3 (MT5 bridge)** — caminho regulado com alavancagem; projeto separado (GG).
8. Bitget, Gate, BingX, KuCoin, dYdX — sob demanda. **MEXC: não recomendado** (risco de congelamento por bot).

---

## 8. Como adicionar uma nova exchange

1. **Crie o adaptador** em `backend/app/exchanges/<id>.py` herdando de `ExchangeAdapter`:
   - defina `exchange_id` (ex.: `"okx_swap"`), `display_name`, `experimental = True` e `capabilities`;
   - construtor com a assinatura `(api_key, api_secret, testnet=None, ..., transport=None)` — `transport` permite
     injetar `httpx.MockTransport` nos testes;
   - implemente **todos** os métodos abstratos (o Python recusa instanciar se faltar algum) devolvendo o
     **formato canônico** (docstring de `base.py`); o que a exchange não suportar levanta
     `UnsupportedFeatureError`;
   - converta erros nativos para `ExchangeAPIError` com os códigos canônicos (`ERR_ORDER_NOT_FOUND`,
     `ERR_CANCEL_REJECTED`, `ERR_NO_CHANGE`, `ERR_INVALID_SYMBOL`) — o reconciliador e os *setters* idempotentes
     dependem disso;
   - `place_order` **deve** enviar o `client_order_id` (idempotência) e rejeitar parâmetros desconhecidos em vez
     de ignorá-los silenciosamente (veja `IGNORED_EXTRAS` em `bybit.py`);
   - se a exchange usa **contratos/lotes**, converta para quantidade em moeda-base na fronteira do adaptador
     (ou adicione `contract_size` a `SymbolFilters` com testes no `RiskManager`).
2. **Registre** em `registry._ensure_builtin()` (ou chame `register_exchange(MinhaClasse)`).
3. **Configuração:** URLs (mainnet/testnet) e `recvWindow` em `app/core/config.py` e `.env.example`. Nunca
   credenciais no código; as chaves da conta continuam criptografadas (AES-256-GCM) em `accounts`.
4. **Streams:** implemente `user_data_stream()` emitindo `ORDER_TRADE_UPDATE`/`ACCOUNT_UPDATE` canônicos (reuse
   `BinanceStream` de `binance_ws.py` para reconexão com backoff, alerta de desconexão e métricas) e
   `market_stream()`. Enquanto não existir, o `StreamSupervisor` pula a conta com um aviso.
5. **Testes** em `backend/tests/test_exchanges.py` (ou um arquivo próprio) **sempre com HTTP mockado**: assinatura,
   tradução de klines/filtros/tickers/posições/ordens, mapeamento de status e de erros, parâmetros recusados.
   Nenhum teste pode chamar a rede nem enviar ordens reais.
6. **Validação manual:** Testnet/demo da exchange com `EXPERIMENTAL_EXCHANGES_ENABLED=true`, conta em
   `execution_mode=paper` → depois `live` na **Testnet** (`LIVE_TRADING_ENABLED=true` só no ambiente de teste).
7. **Promoção a estável:** WS implementado, reconciliação validada por semanas em Testnet, documentação aqui
   atualizada, `experimental = False`, e opção no frontend.
