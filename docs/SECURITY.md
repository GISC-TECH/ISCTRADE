# Segurança

## Segredos
- **Nunca** faça commit de `.env`, chaves de API, certificados ou dumps de banco. O `.gitignore` já cobre esses casos; o repositório contém apenas `.env.example` com placeholders `CHANGE_ME`.
- Gere `JWT_SECRET` e `MASTER_ENCRYPTION_KEY` com fontes criptograficamente seguras (veja o README).
- Faça rotação periódica do `JWT_SECRET` (invalida sessões) e planeje rotação da `MASTER_ENCRYPTION_KEY` (requer re-criptografar as chaves armazenadas).

## Chaves da Binance
- Crie chaves **dedicadas** para a plataforma com permissões **Leitura + Futuros**. **Nunca** habilite saque (*withdrawals*).
- Ative **restrição por IP** (IP do servidor).
- As chaves são armazenadas com **AES-256-GCM** (nonce aleatório de 96 bits, autenticação via tag GCM, `workspace_id` como associated data). A API nunca devolve as chaves — apenas os 4 últimos caracteres.
- Descriptografia ocorre apenas no momento do uso (worker), nunca é logada.

## Autenticação e autorização
- Senhas com **bcrypt**.
- JWT de acesso (curta duração) e refresh (longa duração) em cookies **httpOnly**, `SameSite=Lax`, `Secure` em produção.
- RBAC com papéis `admin`, `trader`, `viewer`; isolamento de dados por workspace em todas as consultas.
- Rate limiting via Redis nos endpoints sensíveis (login/registro).

## Infraestrutura
- Apenas o Caddy expõe portas públicas em produção (80/443). PostgreSQL e Redis ficam na rede interna.
- Headers de segurança (HSTS, `X-Frame-Options`, `nosniff`) configurados no Caddy.
- Containers rodam como usuário não-root.
- Backups regulares e criptografados do PostgreSQL (`scripts/backup_db.sh` é um ponto de partida).

## Risco operacional
- Toda ordem passa pelo `RiskManager` (stop loss obrigatório, risco por trade, alavancagem, perda diária, kill switch por drawdown).
- Ordens *reduce-only* são sempre permitidas para que posições possam ser encerradas.
- Contas em estado `killed` exigem revisão manual antes de serem reiniciadas.

## Reporte de vulnerabilidades
Envie relatos de forma privada aos mantenedores (não abra issues públicas com detalhes exploráveis).
