# ---- deps ----
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- build ----
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# URL pública da API usada pelo navegador. Relativa por padrão: o Caddy encaminha /api/* para o backend.
ARG NEXT_PUBLIC_API_URL=/api/v1
# Destino dos rewrites /api/* do Next.js (gravado no build). Útil quando o frontend é acessado sem o Caddy.
ARG BACKEND_INTERNAL_URL=http://backend:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    BACKEND_INTERNAL_URL=${BACKEND_INTERNAL_URL} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- runtime ----
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
