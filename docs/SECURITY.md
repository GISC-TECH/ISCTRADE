# Segurança

## Modelo de ameaças (resumo)
| Ativo | Ameaça principal | Mitigação |
|---|---|---|
| Chaves de API da Binance | Vazamento do banco/backup | AES-256-GCM com chave mestra fora do banco; chaves sem permissão de saque e com whitelist de IP |
| Capital nas contas | Bug de estratégia, ordem indevida | Paper por padrão, dupla trava para ordens reais, RiskManager obrigatório, kill switches |
| Sessões de usuários | Roubo de token, XSS, força bruta | Cookies `httpOnly`/`Secure`/`SameSite=Lax`, revogação por `token_version`, rate limiting, bcrypt |
| Dados de outros tenants | Acesso entre workspaces | Filtro por `workspace_id` em todas as consultas, RBAC, AAD por workspace na criptografia |

## Segredos
- **Nunca** faça commit de `.env`, chaves de API, certificados ou dumps de banco. O `.gitignore` cobre esses casos; o repositório contém apenas `.env.example` com placeholders `CHANGE_ME`.
- Gere `JWT_SECRET` (≥ 32 caracteres) e `MASTER_ENCRYPTION_KEY` (base64 de 32 bytes) com fontes criptograficamente seguras (veja o README).
- Em `ENVIRONMENT=production` a API **recusa iniciar** com `JWT_SECRET`/`MASTER_ENCRYPTION_KEY` fracos ou placeholders, `COOKIE_SECURE=false` ou `DEBUG=true`; o `scripts/deploy.sh` faz as mesmas verificações antes do deploy.
- **Rotação do `JWT_SECRET`**: troque o valor e reinicie `backend`, `celery_worker`, `celery_beat` e `streams` — todas as sessões são invalidadas.
- **Rotação da `MASTER_ENCRYPTION_KEY`**: ainda não há comando automático. Procedimento: pare os serviços, reconecte as contas (ou re-criptografe com script próprio usando a chave antiga e a nova) e só então descarte a chave antiga. Perder a chave torna as chaves armazenadas irrecuperáveis (será necessário reconectar as contas).
- Backups (`./backups`) contêm as chaves **criptografadas**; guarde a `MASTER_ENCRYPTION_KEY` separada dos backups.

## Chaves da Binance
- Crie chaves **dedicadas** para a plataforma com permissões **Leitura + Futuros**. **Nunca** habilite saque (*withdrawals*).
- Ative **restrição por IP** (IP público do servidor).
- As chaves são armazenadas com **AES-256-GCM** (nonce aleatório de 96 bits, tag de autenticação GCM, `workspace_id` como *associated data*). A API nunca devolve as chaves — apenas os 4 últimos caracteres.
- A descriptografia ocorre somente no momento do uso; chaves e assinaturas nunca são registradas em log nem na auditoria (campos sensíveis são mascarados).

## Execução de ordens
- **Padrão seguro**: contas novas usam Testnet e `execution_mode=paper` (preenchimento simulado). Nenhuma ordem chega à corretora.
- Ordens reais exigem **duas** travas: `LIVE_TRADING_ENABLED=true` no servidor **e** `execution_mode=live` na conta. Com qualquer uma desligada, o executor de paper é usado.
- Toda ordem passa pelo `RiskManager`: kill switch por drawdown, perda diária, stop loss obrigatório, alavancagem máxima, risco por trade e margem disponível. Ordens *reduce-only* são sempre permitidas para que posições possam ser encerradas.
- Ordens reais são acompanhadas de um `STOP_MARKET` *reduce-only* de proteção.
- **Kill switch por conta**: drawdown acima do limite muda a conta para `killed`, para os bots e (com `KILL_SWITCH_CLOSE_POSITIONS=true`) encerra as posições. Só um admin do workspace pode resetar.
- **Kill switch global** (super-admin, painel Admin): bloqueia novas ordens de abertura em todas as contas.
- **Trading manual** (`/api/v1/trading/*`, papel `trader`/`admin`): mesmo pipeline dos bots (kill switches →
  filtros → `RiskManager` → executor do modo da conta), stop loss obrigatório, `confirm=true` explícito e diálogo de
  confirmação na UI; com kill switch só fechar/cancelar é permitido. Toda ação é auditada (`manual.*`, com o
  usuário) e gera o alerta `manual_action`. Veja [REALTIME.md](REALTIME.md).
- Os testes automatizados **sempre** simulam a Binance (transportes HTTP/WebSocket falsos); nenhuma ordem real é enviada pela suíte.

## Notícias, LLM e fontes externas
- Tokens opcionais (`LLM_API_KEY`, `CRYPTOPANIC_API_TOKEN`, `X_BEARER_TOKEN`) ficam só no `.env`; a API de status
  informa apenas se cada fonte está ligada. Erros de fontes são registrados só com o tipo da exceção (a URL da
  CryptoPanic contém o token).
- Ao LLM vai **somente** o texto público da notícia; nunca dados de conta, saldos, posições, chaves ou usuários. A
  saída do LLM é tratada como dado não confiável: JSON validado, valores limitados, texto truncado e o prompt
  orienta a ignorar instruções embutidas na notícia. Notícias e o sentimento só podem bloquear/reduzir entradas —
  nunca criam ordens —, e toda ordem continua passando pelo `RiskManager`.
- Feeds RSS são lidos com `defusedxml` (sem expansão de entidades/XXE). Links de notícias abrem com
  `rel="noopener noreferrer nofollow"`; o texto é exibido como texto puro (sem HTML).

## Autenticação e autorização
- Senhas com **bcrypt**. Troca de senha e "sair de todos os dispositivos" incrementam `token_version`, revogando todos os JWTs emitidos.
- JWT de acesso (curta duração) e refresh (longa duração) em cookies **httpOnly**, `SameSite=Lax`, `Secure` em produção. O cookie de refresh tem `path=/api/v1/auth`. O cookie `btp_session` é apenas um marcador sem segredo, usado pelo middleware do Next.js para redirecionar ao login.
- Rate limiting (Redis, com fallback em memória) em login e registro. Cadastro fechado por padrão (`REGISTRATION_MODE=closed`); `invite` exige convite de uso único (só o SHA-256 é guardado, vinculável a um e-mail, expira em `INVITE_EXPIRE_HOURS`, revogável); `open` é recusado em produção na inicialização sem `ALLOW_OPEN_REGISTRATION_IN_PRODUCTION=true`. O super-admin é criado pela CLI `python -m app.cli create-admin` (senha por prompt/stdin/`BTP_ADMIN_PASSWORD`, nunca na linha de comando); em produção o cadastro nunca concede super-admin e é recusado enquanto não há usuários (sem corrida pelo primeiro cadastro).
- RBAC por workspace: `admin` (gerencia contas, membros, reset de kill switch), `trader` (opera contas e bots), `viewer` (somente leitura). O dono do workspace é sempre `admin`. O papel global `admin` (super-admin) acessa o painel de administração.
- Isolamento de dados por workspace em todas as consultas; o header `X-Workspace-ID` é validado contra a associação do usuário.
- **2FA (TOTP)** — veja a seção abaixo.
- **WebSocket em tempo real** (`/api/v1/ws`): autenticado pelo mesmo cookie httpOnly (com checagem de
  `token_version`), `Origin` obrigatoriamente em `CORS_ORIGINS` (contra *cross-site WebSocket hijacking*), só o
  canal do workspace do qual o usuário é membro (revalidado a cada 60 s; fechamento `4401` ao expirar o token),
  limite de conexões por usuário e de assinaturas por conexão, mensagens do cliente limitadas a 4 KB. Os eventos
  carregam apenas dados já visíveis na API do workspace (nunca chaves, segredos ou tokens); alertas sem workspace
  (falhas de tarefas, streams) não são transmitidos.

## Autenticação em dois fatores (TOTP)
- **Padrão RFC 6238** (SHA-1, 6 dígitos, 30 s, tolerância de ±1 janela) implementado sem dependências externas
  (`app/core/totp.py`); o QR code é gerado no servidor como SVG (`qrcode`). Compatível com Google Authenticator,
  Aegis, 1Password, Authy etc.
- **Segredo cifrado** com a mesma chave AES-256-GCM das credenciais (`MASTER_ENCRYPTION_KEY`), com AAD ligado ao
  usuário (`totp:<user_id>`) — um segredo copiado para outra linha não decifra. O segredo pendente (cadastro ainda não
  confirmado) fica em coluna separada e só vira ativo após um código válido.
- **Anti-replay**: o último passo de tempo aceito é gravado (`totp_last_step`); o mesmo código não serve duas vezes.
- **Códigos de recuperação**: 10 códigos `XXXXX-XXXXX` exibidos uma única vez, guardados como SHA-256 com sal
  individual e removidos ao serem usados; podem ser regenerados (exige código).
- **Login em duas etapas**: com 2FA ativo, `POST /auth/login` não emite cookies; devolve um `mfa_token` (JWT tipo
  `mfa`, válido por `TOTP_CHALLENGE_MINUTES`) que precisa ser enviado com o código em `POST /auth/login/2fa`.
- **Limite de tentativas**: além do rate limit por IP, cada usuário tem orçamento de `TOTP_MAX_FAILURES` códigos
  errados por 15 min (depois `429`), em login, desativação e regeneração.
- **Quando é obrigatório**:
  - super-admins, para usar `/admin` (`TOTP_REQUIRED_FOR_ADMINS=true`);
  - quem tem papel `trader`/`admin` num workspace com conta **live**, quando `LIVE_TRADING_ENABLED=true` e
    `TOTP_REQUIRED_FOR_LIVE=true`: sem 2FA a API responde `403` com o header `X-MFA-Enrollment-Required` e a interface
    leva à página **Segurança**; criar/ativar/iniciar conta live, iniciar bot live, enviar ordem e alterar alavancagem
    em conta live também exigem 2FA. Fechar posições e cancelar ordens continuam liberados (reduzem risco);
  - todos os usuários, se o super-admin ativar a política em **Admin → Política de 2FA**.
- **Desativar** exige senha + código (TOTP ou recuperação) e é recusado enquanto o 2FA for obrigatório para a pessoa.
  Um super-admin pode **redefinir o 2FA** de outro usuário (perda do celular e dos códigos): a ação incrementa
  `token_version` (encerra todas as sessões) e fica na auditoria.
- Eventos auditados: `auth.2fa_setup_started`, `auth.2fa_enabled`, `auth.2fa_disabled`,
  `auth.2fa_recovery_regenerated`, `auth.login_2fa_failed`, `admin.user_2fa_reset` e `admin.security_policy_updated`.

## Infraestrutura
- Apenas o Caddy expõe portas públicas em produção (80/443). PostgreSQL, Redis, backend e frontend ficam em redes internas (o override de produção remove as portas publicadas).
- Headers de segurança (HSTS, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`) configurados no Caddy; documentação interativa da API desabilitada em produção.
- Containers rodam como usuário não-root, com health checks.
- Migrações executadas por um serviço dedicado (`migrate`) antes de API e workers; o `deploy.sh` faz backup do banco antes de migrar.
- Mantenha o host atualizado, com firewall liberando apenas 22/80/443 e acesso SSH por chave.

## Trilha de auditoria à prova de adulteração
Toda linha de `audit_logs` é **selada** em uma cadeia de hashes (`app/services/audit_chain.py`, migração `0009`):

- `log_event` grava a linha sem selo (sem lock no caminho crítico); a tarefa `audit.seal` (beat, a cada
  `AUDIT_SEAL_SECONDS`, padrão 30 s) atribui um `chain_seq` contínuo, o `prev_hash` da linha anterior e
  `row_hash = HMAC-SHA256(chave, "v1|" + prev_hash + "|" + JSON canônico da linha)`. Selagens concorrentes são
  serializadas por *advisory lock* do PostgreSQL e `chain_seq` é `UNIQUE` (a cadeia não bifurca).
- No PostgreSQL um *trigger* recusa `UPDATE` de linha selada. As colunas `user_id`/`workspace_id`/`account_id`
  deixaram de ter FK (um `CASCADE`/`SET NULL` reescreveria linhas auditadas).
- A chave HMAC **não fica no banco** (`AUDIT_CHAIN_KEY` ou derivada da `MASTER_ENCRYPTION_KEY`): quem só tem acesso
  de escrita ao banco não consegue refazer uma cadeia válida depois de editar/apagar linhas.
- Verificação: `GET /api/v1/admin/audit/verify` (super-admin; `?seal=true` sela antes; `anchor_seq`+`anchor_hash`
  conferem uma âncora externa) ou `docker compose exec backend python -m app.audit_cli verify` (código de saída 1
  se a cadeia estiver quebrada). A tarefa `audit.verify` roda diariamente (03:17 UTC) e registra
  `audit.chain_broken` (crítico) se encontrar problema. Erros reportados: `hash_mismatch` (linha editada),
  `gap`/`broken_link` (linha apagada/reordenada), `bad_genesis`, `anchor_mismatch`/`anchor_missing`.
- **Âncora externa** (recomendado): guarde periodicamente fora do servidor a saída de
  `python -m app.audit_cli head` (`head_seq`, `head_hash`); cada selagem também registra o hash da cabeça no log.
  Isso detecta até quem tenha a chave e reconstrua a cadeia inteira.
- Limites: linhas ainda não seladas (últimos segundos) não estão protegidas; apagar um **prefixo** (retenção) é
  reportado como `truncated_prefix` (não é erro, mas fica visível); rotacionar a `MASTER_ENCRYPTION_KEY` sem fixar
  antes `AUDIT_CHAIN_KEY` invalida a verificação (confira com `python -m app.audit_cli key-fingerprint`). Um
  superusuário do PostgreSQL pode desativar o trigger — a proteção real é o HMAC + âncora externa.

## Resposta a incidentes (runbook resumido)
Procedimentos completos (go-live, alertas, backups/restauração, monitor externo, simulado do kill switch):
[RUNBOOK.md](RUNBOOK.md).

1. **Pare novas ordens**: ative o kill switch global no painel Admin (ou `POST /api/v1/admin/kill-switch`).
2. **Encerre posições** se necessário diretamente na Binance.
3. **Revogue as chaves de API** na Binance se houver suspeita de vazamento; reconecte com chaves novas depois.
4. **Revogue sessões**: troque o `JWT_SECRET` e reinicie os serviços.
5. **Investigue** pela trilha de auditoria (Admin → Auditoria global) e pelos logs (`docker compose logs`).
6. **Restaure** um backup se houver corrupção de dados (`scripts/restore_db.sh`, veja [RUNBOOK.md](RUNBOOK.md#restauracao)).

## Limitações conhecidas
- 2FA apenas por TOTP (sem WebAuthn/passkeys). Quem perder o autenticador e os códigos de recuperação depende de um super-admin para redefinir.
- A trilha de auditoria fica no mesmo banco, mas é encadeada com HMAC e verificável (veja acima); não é WORM.
- Sem rotação automática da `MASTER_ENCRYPTION_KEY`.
- Alertas via WhatsApp, Telegram e e-mail (veja [ALERTS.md](ALERTS.md)). Tokens e senha SMTP ficam só no `.env`; o worker Celery e o `streams` expõem métricas sem autenticação nas portas internas 9101/9102 (não publique essas portas).

## Reporte de vulnerabilidades
Envie relatos de forma privada aos mantenedores (não abra issues públicas com detalhes exploráveis). Inclua passos de reprodução e impacto; responderemos o mais rápido possível.
