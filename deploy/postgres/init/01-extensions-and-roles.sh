#!/bin/bash
set -euo pipefail

# Только при первой инициализации тома.
# Расширения — суперпользователь; таблицы — роль estafeta_owner (Alembic).

APP_PASSWORD="${ESTAFETA_APP_PASSWORD:?ESTAFETA_APP_PASSWORD не задан}"
OWNER_PASSWORD="${ESTAFETA_OWNER_PASSWORD:?ESTAFETA_OWNER_PASSWORD не задан}"

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=owner_password="$OWNER_PASSWORD" \
    --set=app_password="$APP_PASSWORD" \
    --set=db_name="$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE estafeta_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE estafeta_app LOGIN PASSWORD :'app_password';

ALTER DATABASE :"db_name" OWNER TO estafeta_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
ALTER SCHEMA public OWNER TO estafeta_owner;
REVOKE ALL ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO estafeta_app;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO estafeta_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO estafeta_owner;
GRANT USAGE ON SCHEMA public TO estafeta_app;
ALTER DEFAULT PRIVILEGES FOR ROLE estafeta_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO estafeta_app;
ALTER DEFAULT PRIVILEGES FOR ROLE estafeta_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO estafeta_app;
SQL
