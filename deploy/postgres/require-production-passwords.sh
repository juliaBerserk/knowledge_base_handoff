#!/bin/bash
set -euo pipefail

declare -A seen=()
names=(POSTGRES_PASSWORD ESTAFETA_OWNER_PASSWORD ESTAFETA_APP_PASSWORD
       ESTAFETA_ADMIN_PASSWORD ESTAFETA_WORKER_PASSWORD)
for name in "${names[@]}"; do
    value="${!name:-}"
    if [[ ! "$value" =~ ^[A-Za-z0-9_-]{20,}$ ]]; then
        echo "Задайте секрет $name в формате Base64URL длиной от 20 символов." >&2
        exit 1
    fi
    if [[ -n "${seen[$value]:-}" ]]; then
        echo "Для каждой роли PostgreSQL нужен отдельный пароль." >&2
        exit 1
    fi
    seen["$value"]=1
done

exec /usr/local/bin/docker-entrypoint.sh "$@"
