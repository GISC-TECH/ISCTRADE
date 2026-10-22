# Agendador de backups (serviço "backup", perfil "backup"). Mesma versão major do servidor para o pg_dump.
FROM postgres:15-alpine
RUN apk add --no-cache bash age rclone gnupg curl coreutils tzdata
COPY backup-loop.sh /usr/local/bin/backup-loop
COPY backup-healthcheck.sh /usr/local/bin/backup-healthcheck
RUN chmod +x /usr/local/bin/backup-loop /usr/local/bin/backup-healthcheck
# scripts/ do repositório é montado em /repo/scripts (docker-compose.yml): uma única implementação do backup.
WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/backup-loop"]
