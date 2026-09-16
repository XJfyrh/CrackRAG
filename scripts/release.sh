#!/bin/sh
# Docker-only Linux entry point. New paid requests always remain server guarded.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export CRACKRAG_STATE=${CRACKRAG_STATE:-"$ROOT/.release"}
export CRACKRAG_PROJECT=${CRACKRAG_PROJECT:-crackrag-release}
export CRACKRAG_HOST_UID=${CRACKRAG_HOST_UID:-$(id -u)}
case "$CRACKRAG_PROJECT" in crackrag-release*) ;; *) echo 'Use a dedicated crackrag-release* project name.' >&2; exit 2;; esac
mkdir -p "$CRACKRAG_STATE"
CRACKRAG_STATE=$(CDPATH= cd -- "$CRACKRAG_STATE" && pwd)
export CRACKRAG_STATE
compose() {
 if [ -f "$CRACKRAG_STATE/compose.env" ]; then
  docker compose --project-name "$CRACKRAG_PROJECT" --project-directory "$ROOT/deploy" --env-file "$CRACKRAG_STATE/compose.env" -f "$ROOT/deploy/compose.release.yaml" "$@"
 else
  docker compose --project-name "$CRACKRAG_PROJECT" --project-directory "$ROOT/deploy" -f "$ROOT/deploy/compose.release.yaml" "$@"
 fi
}
admin() { compose --profile tools run --rm --no-deps admin "$@"; }
ready() { [ -f "$CRACKRAG_STATE/compose.env" ] || { echo 'Run ./crackrag init first.' >&2; exit 2; }; }
action=${1:-help}; [ "$#" -eq 0 ] || shift
case "$action" in
 init)
  if [ "$#" -eq 0 ]; then compose build admin
  elif [ "$#" -ne 1 ] || [ "$1" != --no-build ]; then echo 'Usage: ./crackrag init [--no-build]' >&2;exit 2;fi
  admin verify; admin init; admin volume-init;;
 build) compose build admin;;
 up) ready; admin verify; admin volume-init; compose up -d --wait --wait-timeout 1200 postgres redis runtime api;;
 mock) ready; admin mock; compose stop api runtime api2 runtime2; admin volume-init; compose up -d --wait --wait-timeout 1200 postgres redis runtime api;;
 opening-new) ready; admin opening-new "$@";;
 models|model) ready; admin models "$@";;
 recovery)
  ready
  case "${1:-status}" in
   up) admin verify; admin volume-init; compose --profile recovery up -d --wait --wait-timeout 1200 postgres redis runtime2 api2;;
   stop) compose --profile recovery stop api2 runtime2;;
   status) compose --profile recovery ps;;
   *) echo 'Usage: ./crackrag recovery up|stop|status' >&2;exit 2;;
  esac;;
 demo-recovery)
  ready
  cleanup_demo() { admin mock; compose --profile recovery stop api2 runtime2; compose up -d --wait --wait-timeout 1200 postgres redis runtime api; }
  trap cleanup_demo EXIT
  admin mock --scenario pause_after_candidates
  compose stop api runtime api2 runtime2
  admin verify; admin volume-init
  compose up -d --wait --wait-timeout 1200 postgres redis runtime api
  compose --profile recovery up -d --wait --wait-timeout 1200 postgres redis runtime2 api2
  admin demo-start
  compose kill --signal SIGKILL api runtime
  compose restart redis
  admin demo-finish
  cleanup_demo; trap - EXIT;;
 price) ready; [ "${1:-}" != refresh ] || shift; admin price "$@";;
 live)
  ready; operation=${1:-help}; [ "$#" -eq 0 ] || shift
  case "$operation" in
   prepare) admin pause; compose stop api runtime api2 runtime2; admin prepare "$@";;
   enable) compose up -d --wait --wait-timeout 1200 postgres redis runtime api; admin enable;;
   pause) admin pause;;
   *) echo 'Usage: ./crackrag live prepare --opening /release/opening.json --confirm-exclusive | enable | pause' >&2; exit 2;;
  esac;;
 status) ready; compose ps; compose exec -T postgres psql -X -U crackrag -d crackrag -At -c "WITH local AS (  SELECT COALESCE(sum(amount_cny),0) AS known,         COALESCE(sum(reserved_upper_cny) FILTER(WHERE state<>'SETTLED'),0) AS retained,         count(*) AS attempts FROM llm_calls WHERE provider='deepseek' ), opening AS (  SELECT COALESCE(max(known_cny),0) AS known,COALESCE(max(retained_cny),0) AS retained  FROM release_opening_balance ) SELECT jsonb_build_object('opening_known_cny',opening.known,'opening_retained_cny',opening.retained,  'local_known_cny',local.known,'local_unresolved_cny',local.retained,'local_paid_attempts',local.attempts,  'project_occupied_cny',opening.known+opening.retained+local.known+local.retained,  'project_remaining_cny',greatest(0,100-opening.known-opening.retained-local.known-local.retained)) FROM local CROSS JOIN opening; ";;
 logs) ready; compose logs --tail 100 "${1:-api}";;
 stop) ready; admin pause; compose stop api runtime api2 runtime2 redis postgres;;
 activate)
  ready; admin pause; compose stop api runtime api2 runtime2; compose up -d --wait postgres
  name=$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$CRACKRAG_STATE/state/activations"
  umask 027
  sql="$CRACKRAG_STATE/state/activations/$name.sql";audit="$CRACKRAG_STATE/state/activations/$name.jsonl"
  [ ! -e "$sql" ] && [ ! -e "$audit" ] || { echo 'Activation audit exists' >&2;exit 2; }
  admin activation-sql > "$sql"
  compose exec -T postgres psql -X -U crackrag -d crackrag -v ON_ERROR_STOP=1 -At < "$sql" > "$audit"
  cat "$audit"
  echo 'Catalog activated; history retained. Services remain stopped and paid mode paused.';;
 backup)
  ready; name=${1:-$(date -u +%Y%m%dT%H%M%SZ)};case "$name" in ''|*[!A-Za-z0-9_-]*) echo 'Invalid backup name' >&2;exit 2;;esac
  [ ! -e "$CRACKRAG_STATE/backups/$name" ] || { echo 'Backup exists' >&2;exit 2; }
  admin pause; compose stop api runtime api2 runtime2; compose up -d --wait postgres
  mkdir -p "$CRACKRAG_STATE/backups/$name"
  compose exec -T postgres pg_dump -U crackrag -d crackrag -Fc --no-owner --no-privileges > "$CRACKRAG_STATE/backups/$name/database.dump"
  admin backup-files --name "$name"
  echo 'Applications remain stopped. Run ./crackrag up while the session is valid, or mock when expired; paid mode remains paused.';;
 restore)
  ready; name=${1:-};case "$name" in ''|*[!A-Za-z0-9_-]*) echo 'A backup name is required' >&2;exit 2;;esac
  admin pause; compose stop api runtime api2 runtime2; compose up -d --wait postgres redis
  tables=$(compose exec -T postgres psql -X -U crackrag -d crackrag -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
  [ "$tables" = 0 ] || { echo 'Restore requires a fresh empty database; existing data is never overwritten.' >&2;exit 2; }
  admin volume-init; admin restore-files --name "$name"
  compose exec -T postgres pg_restore -U crackrag -d crackrag --no-owner --no-privileges --exit-on-error < "$CRACKRAG_STATE/backups/$name/database.dump"
  echo 'Restored paused/mock. Run ./crackrag up, verify facts and balances, then explicitly prepare a new live session.';;
 *) echo 'Usage: ./crackrag init | build | up | mock | opening-new --project-id ID | models [--offline] [--smoke] | price refresh | live prepare/enable/pause | recovery up/stop/status | demo-recovery | activate | status | logs | backup [name] | restore name | stop';;
esac
