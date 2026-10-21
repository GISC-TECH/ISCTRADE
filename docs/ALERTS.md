# Alertas via WhatsApp

A plataforma envia alertas operacionais (kill switch, limites de risco, ordens rejeitadas, erros de bots,
WebSocket desconectado, divergências do reconciliador, falhas de tarefas e resumo diário de PnL) para números
de WhatsApp. Todo alerta é gravado no histórico (tabela `alerts`, tela **Monitoramento**) mesmo quando o
WhatsApp não está configurado.

> **Nunca coloque tokens reais no repositório.** As credenciais ficam somente no `.env` do servidor
> (que é ignorado pelo git). Mensagens e logs passam por mascaramento de segredos e os números aparecem
> mascarados no histórico (`+5511*****1234`).

## Como funciona

```
evento (auditoria / WebSocket / Celery / beat)
   └─▶ alerta enfileirado (só após o COMMIT da transação; descartado em rollback)
         └─▶ Celery "alerts.process": configurações do workspace, evento ligado?, severidade mínima,
               antispam (Redis SET NX EX) ─▶ grava em `alerts`
               └─▶ Celery "alerts.deliver": envia a cada destinatário pelo provedor configurado
                     (até 5 novas tentativas com backoff exponencial; números já atendidos não recebem de novo)
```

- O envio é **assíncrono**: o trading nunca espera nem falha por causa de um alerta. Se o broker estiver fora
  do ar o alerta é descartado (contador `btp_alerts_total{status="dispatch_failed"}`).
- **Destinatários**: os números do workspace (tela Monitoramento). Se o workspace não tiver números, usa
  `WHATSAPP_TO`. Alertas globais (kill switch global, falha de tarefa, WebSocket de mercado) vão para
  `WHATSAPP_TO` **e** para os workspaces que mantêm o evento ligado.
- **Antispam**: um alerta por evento/conta/motivo a cada `ALERT_THROTTLE_MINUTES` (padrão 15; configurável por
  workspace). Kill switch global, ordens executadas, posições e resumo diário não são agrupados.

### Eventos

| Evento | Severidade | Padrão | Origem |
|---|---|---|---|
| `kill_switch_account` — kill switch da conta | 🚨 crítico | ligado | `risk.kill_switch` |
| `kill_switch_global` — kill switch global (ativado/desativado) | 🚨 crítico | ligado | `admin.global_kill_switch` |
| `risk_limit` — perda diária / drawdown atingido | 🚨 crítico | ligado | ordem rejeitada por `max_daily_loss`, `max_drawdown` ou `kill_switch` |
| `order_rejected` — ordem rejeitada pelo risco | ⚠️ aviso | ligado | `order.rejected` (demais motivos) |
| `order_filled` — ordem executada | ℹ️ info | desligado | `order.filled` / `order.submitted` |
| `position_opened` / `position_closed` | ℹ️ info | desligado | fills em paper; stop loss / take profit simulados |
| `bot_error` — bot em erro | ⚠️ aviso | ligado | `bot.error` |
| `ws_disconnected` — WebSocket fora por mais de `ALERT_WS_DISCONNECT_SECONDS` (e reconexão ✅) | ⚠️ aviso | ligado | worker `streams` |
| `reconciler_mismatch` — posições corrigidas pelo reconciliador | ⚠️ aviso | ligado | `reconcile.changes` |
| `task_failed` — falha em tarefa Celery | ⚠️ aviso | ligado | sinal `task_failure` |
| `daily_summary` — resumo diário de PnL | 📊 info | ligado | beat `alerts.daily_summary` (`ALERT_DAILY_SUMMARY_HOUR_UTC`, minuto 5) |
| `news_high_impact` — notícia de alto impacto em símbolo operado | ⚠️ aviso | ligado | `news.ingest` ([NEWS.md](NEWS.md)); uma vez por notícia, só para workspaces expostos ao símbolo |
| `economic_event` — evento econômico de alto impacto em breve | ℹ️ info | ligado | beat `calendar.alerts` (`ECON_ALERT_LEAD_MINUTES` antes) |
| `bot_event_pause` — bot pausou novas entradas por evento | ℹ️ info | ligado | `bot.event_pause` |
| `manual_action` — ação manual de trading (ordem, fechar, fechar todas, cancelar, alavancagem, tentativa bloqueada por kill switch) | ℹ️ info / ⚠️ aviso se rejeitada ou bloqueada | ligado | `manual.*` ([REALTIME.md](REALTIME.md)) |

Exemplo de mensagem:

```
🚨 *ISCTRADE · Limite de drawdown/perda diária atingido*
• Workspace: Mesa principal
• Conta: Binance 01
• Modo: paper / testnet
• Símbolo: BTCUSDT
• Motivo: limite de perda diária atingido (max_daily_loss: 3.10% >= 3%)
🕒 20/10/2026 14:05 UTC · severidade: critical
```

## Variáveis de ambiente

| Variável | Uso |
|---|---|
| `ALERTS_ENABLED` | Liga/desliga todo o pipeline de alertas (padrão `true`). |
| `ALERT_THROTTLE_MINUTES` | Janela antispam padrão (min). |
| `ALERT_WS_DISCONNECT_SECONDS` | Tempo de WebSocket desconectado antes de alertar (padrão 120). |
| `ALERT_DAILY_SUMMARY_HOUR_UTC` | Hora UTC do resumo diário (padrão 0). |
| `WHATSAPP_ENABLED` | `true` para enviar mensagens (padrão `false`: alertas só no histórico). |
| `WHATSAPP_PROVIDER` | `wago` (padrão) \| `wago_api` \| `meta_cloud` \| `evolution` \| `generic_http`. |
| `WHATSAPP_API_URL` | URL base do provedor (`wago`: vazio = `https://wago-api.hablame.co/v1`). |
| `WHATSAPP_API_TOKEN` | Token/API key do provedor (**segredo**). |
| `WHATSAPP_DEVICE_ID` | `wago`: ID do número (device) que envia — opcional. |
| `WHATSAPP_SESSION` (ou `WHATSAPP_INSTANCE`) | `wago_api`: sessão · `evolution`: instância. |
| `WHATSAPP_PHONE_NUMBER_ID` | `meta_cloud`: Phone number ID. |
| `WHATSAPP_AUTH_HEADER` / `WHATSAPP_AUTH_PREFIX` | `wago_api`/`generic_http`: cabeçalho e prefixo do token (padrão `Authorization` + `Bearer `). |
| `WHATSAPP_HTTP_METHOD`, `WHATSAPP_EXTRA_HEADERS`, `WHATSAPP_BODY_TEMPLATE` | `generic_http`. |
| `WHATSAPP_TO` | Destinatários padrão em E.164, separados por vírgula (`+5511999999999,+5521988887777`). |
| `WHATSAPP_TIMEOUT_SECONDS` | Timeout HTTP por mensagem (padrão 10). |

Depois de alterar o `.env`: `docker compose up -d backend celery_worker celery_beat streams`.

## Opção 1 — WAGO hospedado (padrão, `WHATSAPP_PROVIDER=wago`)

[WAGO](https://wago.hablame.co) é um serviço hospedado: você conecta o seu número escaneando um QR code no
painel e usa a API REST. Formato verificado no API tester oficial
(<https://wago.hablame.co/help/api-tester>, exemplo "Send text message", operação `createMessage`):

- `POST https://wago-api.hablame.co/v1/messages`
- Cabeçalho `Token: <API key>`
- Corpo JSON `{"phone": "+5511999999999", "message": "texto", "device": "<device id opcional>"}`

Passo a passo:

1. Crie a conta em <https://wago.hablame.co>, adicione um número e escaneie o QR code
   com o WhatsApp do celular.
2. Copie a **API key** no painel (seção de API/desenvolvedores) e, se tiver mais de um número, o **ID do device** que deve enviar.
3. Teste fora da plataforma:

   ```bash
   curl -sS -X POST "https://wago-api.hablame.co/v1/messages" \
     -H "Token: $WAGO_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"phone": "+5511999999999", "message": "Teste ISCTRADE ✅"}'
   # com um número específico de envio:
   #  -d '{"phone": "+5511999999999", "message": "Teste", "device": "'"$WAGO_DEVICE_ID"'"}'
   ```

4. Configure o `.env`:

   ```dotenv
   WHATSAPP_ENABLED=true
   WHATSAPP_PROVIDER=wago
   WHATSAPP_API_URL=                 # vazio = https://wago-api.hablame.co/v1
   WHATSAPP_API_TOKEN=<sua API key>
   WHATSAPP_DEVICE_ID=               # opcional
   WHATSAPP_TO=+5511999999999
   ```

## Opção 2 — wago-api self-hosted (`WHATSAPP_PROVIDER=wago_api`)

Para quem roda o gateway **wago-api** (baseado em whatsmeow) na própria infraestrutura. Formato usado:

- `POST {WHATSAPP_API_URL}/client/sendMessage/{WHATSAPP_SESSION}`
- Corpo `{"chatId": "5511999999999@s.whatsapp.net", "text": "texto"}`
- Se `WHATSAPP_API_TOKEN` estiver definido, envia `WHATSAPP_AUTH_HEADER: WHATSAPP_AUTH_PREFIX + token`
  (padrão `Authorization: Bearer <token>`).

> Este formato veio da especificação do projeto (o repositório público `github.com/arktnld/wago-api` não estava
> acessível no momento da implementação). Confirme com a versão que você roda; se a rota for diferente use
> `generic_http`.

```bash
# inicie/pareie a sessão conforme a documentação do seu wago-api, depois:
curl -sS -X POST "http://wago-api:8080/client/sendMessage/principal" \
  -H "Authorization: Bearer $WAGO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"chatId": "5511999999999@s.whatsapp.net", "text": "Teste ISCTRADE ✅"}'
```

```dotenv
WHATSAPP_ENABLED=true
WHATSAPP_PROVIDER=wago_api
WHATSAPP_API_URL=http://wago-api:8080     # rede interna do Docker ou URL privada
WHATSAPP_SESSION=principal
WHATSAPP_API_TOKEN=                        # se o seu gateway exigir
WHATSAPP_TO=+5511999999999
```

Para colocar o gateway na mesma rede do compose, adicione o serviço num `docker-compose.override.yml` com
`networks: [backend]` e **não** publique a porta.

## Opção 3 — WhatsApp Cloud API da Meta (`meta_cloud`)

- `POST https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages`, `Authorization: Bearer <token>`.
- Mensagens de texto livres só são entregues dentro da janela de 24 h após o destinatário falar com o número;
  fora dela a Meta exige *templates* aprovados (não suportados por este provedor — use um dos gateways acima).

```bash
curl -sS -X POST "https://graph.facebook.com/v20.0/$PHONE_NUMBER_ID/messages" \
  -H "Authorization: Bearer $META_TOKEN" -H "Content-Type: application/json" \
  -d '{"messaging_product": "whatsapp", "to": "5511999999999", "type": "text", "text": {"body": "Teste ISCTRADE"}}'
```

```dotenv
WHATSAPP_PROVIDER=meta_cloud
WHATSAPP_API_TOKEN=<token permanente do usuário de sistema>
WHATSAPP_PHONE_NUMBER_ID=<phone number id>
```

## Opção 4 — Evolution API (`evolution`)

- `POST {WHATSAPP_API_URL}/message/sendText/{WHATSAPP_INSTANCE}`, cabeçalho `apikey: <token>`,
  corpo `{"number": "5511999999999", "text": "..."}`.

```bash
curl -sS -X POST "http://evolution:8080/message/sendText/minha-instancia" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"number": "5511999999999", "text": "Teste ISCTRADE"}'
```

```dotenv
WHATSAPP_PROVIDER=evolution
WHATSAPP_API_URL=http://evolution:8080
WHATSAPP_INSTANCE=minha-instancia
WHATSAPP_API_TOKEN=<apikey>
```

## Opção 5 — Gateway HTTP genérico (`generic_http`)

Para qualquer outro gateway. `WHATSAPP_BODY_TEMPLATE` é um JSON com os marcadores `{to}` (E.164 com `+`),
`{to_digits}` (só dígitos) e `{text}` (escapado para JSON). A URL também aceita `{to}` e `{session}`.

```dotenv
WHATSAPP_PROVIDER=generic_http
WHATSAPP_API_URL=https://gateway.exemplo.com/api/send
WHATSAPP_HTTP_METHOD=POST
WHATSAPP_AUTH_HEADER=X-Api-Key
WHATSAPP_AUTH_PREFIX=
WHATSAPP_API_TOKEN=<token>
WHATSAPP_EXTRA_HEADERS={"X-Tenant": "isctrade"}
WHATSAPP_BODY_TEMPLATE={"number": "{to_digits}", "message": "{text}"}
```

## Testando pela plataforma

1. Na tela **Monitoramento** (admin do workspace), informe os números em E.164, escolha os eventos e salve.
2. Clique em **Enviar alerta de teste** — o envio é síncrono e o resultado aparece na hora e no histórico.

Pela API (com o token de acesso do login):

```bash
curl -sS -X PUT https://SEU_DOMINIO/api/v1/alerts/settings \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"recipients": ["+5511999999999"], "events": {"order_filled": true}, "min_severity": "info"}'

curl -sS -X POST https://SEU_DOMINIO/api/v1/alerts/test -H "Authorization: Bearer $ACCESS_TOKEN"
curl -sS "https://SEU_DOMINIO/api/v1/alerts/?limit=20" -H "Authorization: Bearer $ACCESS_TOKEN"
curl -sS https://SEU_DOMINIO/api/v1/monitoring/status -H "Authorization: Bearer $ACCESS_TOKEN"
```

Status de entrega: `sent` (todos receberam), `partial`, `failed` (erro em `error`), `pending` (tentando de novo)
e `disabled` (WhatsApp desligado ou sem credenciais — o alerta fica só no histórico).

## Solução de problemas

- **`disabled`**: confira `WHATSAPP_ENABLED=true`, `WHATSAPP_PROVIDER` e as variáveis obrigatórias do provedor.
- **`failed` com HTTP 401/403**: token inválido (não há nova tentativa para erros 4xx, exceto 408/429).
- **Nada chega e não há registro**: verifique o `celery_worker` (`docker compose logs celery_worker`) e se o
  evento está ligado no workspace / acima da severidade mínima; repetições dentro da janela antispam são
  ignoradas de propósito.
- Métricas: `btp_alerts_total{event,status}` (worker) e `btp_alerts_last_24h{status}` (API).
