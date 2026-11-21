#!/usr/bin/env bash
# Deploy to Railway from this checkout (no GitHub Actions needed).
#   scripts/deploy.sh                 # api + workers (backend changes, UI changes)
#   scripts/deploy.sh all             # api workers mockgw simulator sso
#   scripts/deploy.sh sso mockgw      # chosen services
# Waits for every deployment, then smoke-tests the api. Needs `railway login` + `railway link` once.
set -euo pipefail
cd "$(dirname "$0")/.."

src() { case "$1" in api|workers) echo backend ;; mockgw|simulator|sso) echo "$1" ;; esac; }
services=("$@")
[ ${#services[@]} -eq 0 ] && services=(api workers)
[ "${services[0]}" = all ] && services=(api workers mockgw simulator sso)

for s in "${services[@]}"; do
  dir=$(src "$s")
  [ -z "$dir" ] && { echo "unknown service: $s (known: api workers mockgw simulator sso)"; exit 1; }
  echo "→ $s (from $dir/)"
  railway up "$dir" --path-as-root --service "$s" --detach >/dev/null
done

sleep 20
for s in "${services[@]}"; do
  for _ in $(seq 1 60); do
    st=$(railway service status --service "$s" 2>/dev/null | awk '/Status/{print $2}')
    case "$st" in SUCCESS|FAILED|CRASHED) break ;; esac
    sleep 10
  done
  echo "$s: $st"
  [ "$st" = SUCCESS ] || exit 1
done

api=$(railway variables --service api --kv 2>/dev/null | awk -F= '$1=="RAILWAY_PUBLIC_DOMAIN"{print $2}')
[ -n "$api" ] && curl -fsS "https://$api/readyz" && echo && echo "https://$api/"
