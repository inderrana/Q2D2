#!/usr/bin/env bash
# Q2D2 launcher — pulls secrets from `pass` (passwordstore.org) and forwards
# them into docker compose. Keeps secrets out of the repo and out of the
# container image.
#
# One-time setup:
#   sudo apt install pass gnupg
#   gpg --quick-gen-key "you@local"
#   pass init you@local
#   pass insert q2d2/tavily          # paste the key once
#   pass insert q2d2/jwt-secret      # optional; otherwise auto-generated
#   pass insert q2d2/brave           # optional
#   pass insert q2d2/encryption-key  # 32-byte urlsafe base64; auto-gen if absent
#
# Usage:
#   ./run.sh up -d
#   ./run.sh logs -f
#   ./run.sh down
#
set -euo pipefail

_pass_or_empty() { pass show "$1" 2>/dev/null || true; }
_b64key() { python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'; }

# --- Required ---
if ! command -v pass >/dev/null 2>&1; then
  echo "error: 'pass' is not installed. Run: sudo apt install pass gnupg" >&2
  exit 1
fi

export TAVILY_API_KEY="$(_pass_or_empty q2d2/tavily)"
export BRAVE_API_KEY="$(_pass_or_empty q2d2/brave)"
export CLOUDFLARE_API_TOKEN="$(_pass_or_empty q2d2/cloudflare)"

# JWT secret — fall back to one we autogenerate and store in pass on first run.
JWT_SECRET="$(_pass_or_empty q2d2/jwt-secret)"
if [[ -z "${JWT_SECRET}" ]]; then
  JWT_SECRET="$(_b64key)"
  echo "$JWT_SECRET" | pass insert -e q2d2/jwt-secret >/dev/null
  echo "[run.sh] generated and stored JWT secret in pass: q2d2/jwt-secret"
fi
export JWT_SECRET

# Master encryption key for at-rest secrets in the DB (per-user API keys).
ENCRYPTION_KEY="$(_pass_or_empty q2d2/encryption-key)"
if [[ -z "${ENCRYPTION_KEY}" ]]; then
  ENCRYPTION_KEY="$(_b64key)"
  echo "$ENCRYPTION_KEY" | pass insert -e q2d2/encryption-key >/dev/null
  echo "[run.sh] generated and stored encryption key in pass: q2d2/encryption-key"
fi
export ENCRYPTION_KEY

# SearXNG secret_key — autogen + persist on first run.
SEARXNG_SECRET="$(_pass_or_empty q2d2/searxng-secret)"
if [[ -z "${SEARXNG_SECRET}" ]]; then
  SEARXNG_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  echo "$SEARXNG_SECRET" | pass insert -e q2d2/searxng-secret >/dev/null
  echo "[run.sh] generated and stored SearXNG secret in pass: q2d2/searxng-secret"
fi
export SEARXNG_SECRET

# Cookie secure flag: 1 in HTTPS deployments (including local Caddy TLS), 0 only for plain-http.
export COOKIE_SECURE="${COOKIE_SECURE:-1}"

# Auto-detect the Docker socket path for the docker-socket-proxy sidecar
# (used by the admin "Restart Ollama service" action). Honors a pre-set
# override. The socket is mounted into the proxy only, not into q2d2.
if [[ -z "${DOCKER_SOCK_HOST:-}" ]]; then
  if [[ -S "/run/user/${UID}/docker.sock" ]]; then
    DOCKER_SOCK_HOST="/run/user/${UID}/docker.sock"
  else
    DOCKER_SOCK_HOST="/var/run/docker.sock"
  fi
fi
export DOCKER_SOCK_HOST

# Make sure the externally-managed `ollama` container (if present) is on the
# compose network so q2d2 can resolve `http://ollama:11434` via DNS. Safe to
# re-run — `network connect` is idempotent here (we swallow the "already
# exists" error).
_attach_ollama() {
  local net="q2d2_default"
  docker network inspect "$net" >/dev/null 2>&1 || return 0
  docker ps --format '{{.Names}}' | grep -qx ollama || return 0
  docker network connect "$net" ollama 2>/dev/null || true
}

# Install Caddy's local root CA into the system trust store and (if present)
# Firefox NSS DBs. Idempotent. Run after the first `up` so the cert exists.
_trust_ca() {
  local src dst
  src="$(mktemp -t caddy-root.XXXXXX.crt)"
  if ! docker cp q2d2-caddy:/data/caddy/pki/authorities/local/root.crt "$src" 2>/dev/null; then
    echo "[run.sh] caddy container not running or CA not yet issued. Run './run.sh up -d' first." >&2
    rm -f "$src"; return 1
  fi
  dst="/usr/local/share/ca-certificates/caddy-q2d2-root.crt"
  if ! sudo cmp -s "$src" "$dst" 2>/dev/null; then
    sudo cp "$src" "$dst"
    sudo update-ca-certificates >/dev/null
    echo "[run.sh] installed Caddy root CA into system trust store: $dst"
  else
    echo "[run.sh] system trust store already has the Caddy root CA"
  fi
  # Firefox: walk every cert9.db we can find.
  if command -v certutil >/dev/null 2>&1; then
    local found=0
    while IFS= read -r db; do
      [[ -f "$db" ]] || continue
      found=1
      local d; d="$(dirname "$db")"
      certutil -d "sql:$d" -L -n "Caddy Q2D2 Local Authority" >/dev/null 2>&1 \
        || { certutil -A -n "Caddy Q2D2 Local Authority" -t "C,," -i "$src" -d "sql:$d" \
             && echo "[run.sh] added CA to Firefox profile: $d"; }
    done < <(find "$HOME/.mozilla/firefox" "$HOME/snap/firefox/common/.mozilla/firefox" \
                  "$HOME/.var/app/org.mozilla.firefox/.mozilla/firefox" \
                  -maxdepth 3 -name cert9.db 2>/dev/null)
    [[ $found -eq 0 ]] && echo "[run.sh] no Firefox profile found on this host (skipping Firefox trust)"
  else
    echo "[run.sh] 'certutil' not installed; skipping Firefox NSS trust. Install with: sudo apt install libnss3-tools"
  fi
  rm -f "$src"
}

# Custom subcommand: `./run.sh trust-ca`
if [[ "${1:-}" == "trust-ca" ]]; then
  _trust_ca
  exit $?
fi

# WSL / Docker Desktop support: use /var/run/docker.sock when the default
# per-user socket path is unavailable.
if [[ -S "/var/run/docker.sock" ]]; then
  if [[ -z "${DOCKER_HOST:-}" ]]; then
    export DOCKER_HOST="unix:///var/run/docker.sock"
  else
    if [[ "${DOCKER_HOST}" == "unix:///run/user/${UID}/docker.sock" ]] && [[ ! -S "/run/user/${UID}/docker.sock" ]]; then
      export DOCKER_HOST="unix:///var/run/docker.sock"
    fi
  fi
fi

docker compose "$@"
rc=$?
# Only attach after a successful `up` (so DNS works on the next request).
if [[ "${1:-}" == "up" && $rc -eq 0 ]]; then
  _attach_ollama
fi
exit $rc
