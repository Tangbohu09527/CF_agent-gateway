#!/usr/bin/env bash
set -Eeuo pipefail
set +x
# Executed only by the upstream image when its own data volume is empty.
# psql reads the credential from the mounted file, never from command arguments.
psql --username postgres --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\set app_password `cat /run/secrets/app-password`
SELECT format('CREATE ROLE cf_gateway LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cf_gateway') \gexec
SELECT 'CREATE DATABASE cf_gateway OWNER cf_gateway'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cf_gateway') \gexec
SQL
