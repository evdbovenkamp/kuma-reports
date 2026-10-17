#!/usr/bin/env bash
# Kuma Reports installer
#
#   curl -fsSL https://raw.githubusercontent.com/evdbovenkamp/kuma-reports/main/install.sh | sudo bash
#
# Finds your Uptime Kuma container, its data volume and database type, creates a read-only
# database user when Kuma uses MariaDB, writes docker-compose.yml + .env and starts Kuma Reports.
# Run it again to upgrade an existing installation.
set -euo pipefail

REPO="evdbovenkamp/kuma-reports"
IMAGE="${KR_IMAGE:-ghcr.io/evdbovenkamp/kuma-reports:latest}"
INSTALL_DIR="${KR_DIR:-/opt/kuma-reports}"
KUMA_CONTAINER="${KR_KUMA_CONTAINER:-}"
BIND="${KR_BIND:-}"
PORT="${KR_PORT:-}"
BASE_PATH="${KR_BASE_PATH-unset}"
KUMA_URL_ARG="${KR_KUMA_URL:-}"
ASSUME_YES="${KR_YES:-0}"
BUILD_FROM_SOURCE=0

# ------------------------------------------------------------------ helpers
if [ -t 1 ]; then
  B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
info()  { printf '%s\n' "${G}==>${N} ${B}$*${N}"; }
note()  { printf '    %s\n' "$*"; }
warn()  { printf '%s\n' "${Y}warning:${N} $*" >&2; }
die()   { printf '%s\n' "${R}error:${N} $*" >&2; exit 1; }

# Prompts read from the terminal, so the script also works when piped into bash.
TTY=/dev/tty
[ -r "$TTY" ] || ASSUME_YES=1

ask() {  # ask "Question" default -> echoes answer
  local q="$1" def="${2:-}" ans=""
  if [ "$ASSUME_YES" = 1 ]; then echo "$def"; return; fi
  if [ -n "$def" ]; then printf '%s %s ' "${B}?${N} $q" "${D}[$def]${N}" >"$TTY"; else printf '%s ' "${B}?${N} $q" >"$TTY"; fi
  IFS= read -r ans <"$TTY" || true
  echo "${ans:-$def}"
}

confirm() {  # confirm "Question" [Y|N]
  local def="${2:-Y}" ans
  ans="$(ask "$1 (y/n)" "$def")"
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c "${1:-24}"
  else tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-24}"; fi
}

json_get() {  # json_get '{"a":"b"}' key  (flat JSON only, enough for db-config.json)
  printf '%s' "$1" | tr -d '\n' | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"
}

usage() {
  cat <<EOF
Kuma Reports installer

Usage: install.sh [options]

  --dir PATH          Install directory (default: /opt/kuma-reports)
  --container NAME    Uptime Kuma container name (default: auto-detect)
  --bind ADDRESS      Address to publish the GUI on (default: 127.0.0.1)
  --port PORT         Port to publish the GUI on (default: 8080)
  --base-path PATH    Serve the GUI under a sub-path, e.g. /reports behind Kuma's own site (default: none)
  --kuma-url URL      Public URL of Uptime Kuma, for links in reports
  --image IMAGE       Docker image (default: $IMAGE)
  --build             Build the image from source instead of pulling it
  -y, --yes           Accept all defaults, no questions
  -h, --help          Show this help

Environment variables KR_DIR, KR_KUMA_CONTAINER, KR_BIND, KR_PORT, KR_IMAGE and KR_YES=1 work too.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --container) KUMA_CONTAINER="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --base-path) BASE_PATH="$2"; shift 2 ;;
    --kuma-url) KUMA_URL_ARG="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --build) BUILD_FROM_SOURCE=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

printf '\n%s\n\n' "${B}Kuma Reports${N} — scheduled availability & incident reports for Uptime Kuma"

# ------------------------------------------------------------------ prerequisites
command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker info >/dev/null 2>&1 || die "cannot talk to Docker — run as root (sudo) or as a member of the docker group"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (docker compose …)"

# ------------------------------------------------------------------ upgrade path
if [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/.env" ]; then
  info "Existing installation found in $INSTALL_DIR"
  if confirm "Upgrade it to the latest version?" Y; then
    cd "$INSTALL_DIR"
    if grep -q '^\s*build:' docker-compose.yml; then
      [ -d src/.git ] && git -C src pull --ff-only || true
      docker compose up -d --build
    else
      docker compose pull || warn "could not pull a newer image; keeping the current one"
      docker compose up -d
    fi
    info "Upgraded. Logs: docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
    exit 0
  fi
  die "aborted — remove $INSTALL_DIR first for a fresh install"
fi

# ------------------------------------------------------------------ find Uptime Kuma
info "Looking for Uptime Kuma"
if [ -z "$KUMA_CONTAINER" ]; then
  # Image name contains "uptime-kuma" (also custom builds) AND the container has Kuma's /app/data mount —
  # that excludes companions like API bridges whose image names contain "uptime-kuma" too.
  candidates=()
  for c in $(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /uptime-kuma/ {print $1}'); do
    if docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$c" | grep -qw '/app/data'; then
      candidates+=("$c")
    fi
  done
  if [ "${#candidates[@]}" -eq 0 ]; then
    die "no running Uptime Kuma container found — start it, or pass --container NAME"
  elif [ "${#candidates[@]}" -eq 1 ]; then
    KUMA_CONTAINER="${candidates[0]}"
  else
    note "Found: ${candidates[*]}"
    KUMA_CONTAINER="$(ask "Which container is your Uptime Kuma?" "${candidates[0]}")"
  fi
fi
docker inspect "$KUMA_CONTAINER" >/dev/null 2>&1 || die "container '$KUMA_CONTAINER' not found"

KUMA_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$KUMA_CONTAINER")"
KUMA_VERSION="$(docker exec "$KUMA_CONTAINER" node -p "require('/app/package.json').version" 2>/dev/null || echo "?")"
MOUNT="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Type}}|{{.Name}}|{{.Source}}{{end}}{{end}}' "$KUMA_CONTAINER")"
[ -n "$MOUNT" ] || die "container '$KUMA_CONTAINER' has no volume or bind mount on /app/data"
MOUNT_TYPE="${MOUNT%%|*}"; rest="${MOUNT#*|}"; MOUNT_NAME="${rest%%|*}"; MOUNT_SOURCE="${rest#*|}"
note "Container : $KUMA_CONTAINER ($KUMA_IMAGE, version $KUMA_VERSION)"
if [ "$MOUNT_TYPE" = "volume" ]; then note "Data      : volume $MOUNT_NAME"; else note "Data      : $MOUNT_SOURCE"; fi

DB_CONFIG="$(docker exec "$KUMA_CONTAINER" cat /app/data/db-config.json 2>/dev/null || true)"
DB_TYPE="$(json_get "$DB_CONFIG" type)"; DB_TYPE="${DB_TYPE:-sqlite}"
DB_NAME="$(json_get "$DB_CONFIG" dbName)"; DB_NAME="${DB_NAME:-kuma}"
note "Database  : $DB_TYPE"

# ------------------------------------------------------------------ database access
KUMA_DB_USER="reporter"
KUMA_DB_PASSWORD=""
EXTRA_NETWORKS=()
KUMA_BASE_URL=""
# Join Kuma's Docker networks: Kuma can then monitor http://kuma-reports:8080/healthz, and an external
# database container resolves by the same hostname Kuma uses.
mapfile -t EXTRA_NETWORKS < <(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$KUMA_CONTAINER" | grep -v -E '^(bridge|host|none)?$' || true)

case "$DB_TYPE" in
  sqlite)
    note "SQLite is read directly from the data volume (opened read-only); nothing to set up."
    ;;
  embedded-mariadb)
    info "Read-only database user"
    note "Kuma's embedded MariaDB is reached through its socket in the data volume."
    note "A user '$KUMA_DB_USER' with only SELECT rights on '$DB_NAME' will be created (or its password reset)."
    confirm "Create the read-only user now?" Y || die "aborted — create the user yourself, see the README"
    KUMA_DB_PASSWORD="$(random_secret 32)"
    docker exec -i "$KUMA_CONTAINER" mariadb -S /app/data/run/mariadb.sock -u root >/dev/null <<SQL || die "could not create the database user"
CREATE USER IF NOT EXISTS '$KUMA_DB_USER'@'localhost' IDENTIFIED BY '$KUMA_DB_PASSWORD';
ALTER USER '$KUMA_DB_USER'@'localhost' IDENTIFIED BY '$KUMA_DB_PASSWORD';
GRANT SELECT ON \`$DB_NAME\`.* TO '$KUMA_DB_USER'@'localhost';
SQL
    KUMA_BASE_URL="$(docker exec "$KUMA_CONTAINER" mariadb -S /app/data/run/mariadb.sock -u "$KUMA_DB_USER" -p"$KUMA_DB_PASSWORD" -N \
      -e "SELECT value FROM \`$DB_NAME\`.setting WHERE \`key\`='primaryBaseURL'" 2>/dev/null | tr -d '"' || true)"
    note "User created."
    ;;
  mariadb)
    DB_HOST="$(json_get "$DB_CONFIG" hostname)"
    DB_PORT="$(json_get "$DB_CONFIG" port)"; DB_PORT="${DB_PORT:-3306}"
    info "Read-only database user (MariaDB at ${DB_HOST:-?}:$DB_PORT)"

    # Is the database itself a container on this host? Match its name or network alias.
    DB_CONTAINER=""
    for c in $(docker ps -q); do
      names="$(docker inspect -f '{{.Name}} {{range .NetworkSettings.Networks}}{{join .Aliases " "}} {{end}}' "$c" 2>/dev/null | tr -d '/')"
      names="$names $(docker inspect -f '{{range .NetworkSettings.Networks}}{{join .DNSNames " "}} {{end}}' "$c" 2>/dev/null || true)"
      image="$(docker inspect -f '{{.Config.Image}}' "$c")"
      if printf '%s' "$image" | grep -qiE 'mariadb|mysql' && printf ' %s ' "$names" | grep -q " $DB_HOST "; then
        DB_CONTAINER="$(docker inspect -f '{{.Name}}' "$c" | tr -d '/')"; break
      fi
    done

    if [ -n "$DB_CONTAINER" ] && confirm "Database runs in container '$DB_CONTAINER'. Create the read-only user there automatically?" Y; then
      ROOT_PW="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB_CONTAINER" \
                 | sed -n 's/^\(MARIADB_ROOT_PASSWORD\|MYSQL_ROOT_PASSWORD\)=//p' | head -1)"
      if [ -z "$ROOT_PW" ]; then
        [ "$ASSUME_YES" = 1 ] && die "no root password found in '$DB_CONTAINER'; run interactively or create the user manually"
        printf '%s ' "${B}?${N} MariaDB root password for '$DB_CONTAINER':" >"$TTY"; IFS= read -rs ROOT_PW <"$TTY"; echo >"$TTY"
      fi
      CLIENT="$(docker exec "$DB_CONTAINER" sh -c 'command -v mariadb || command -v mysql' 2>/dev/null || true)"
      [ -n "$CLIENT" ] || die "no mariadb/mysql client found in '$DB_CONTAINER'"
      KUMA_DB_PASSWORD="$(random_secret 32)"
      docker exec -i -e MYSQL_PWD="$ROOT_PW" "$DB_CONTAINER" "$CLIENT" -u root >/dev/null <<SQL || die "could not create the user (wrong root password?)"
CREATE USER IF NOT EXISTS '$KUMA_DB_USER'@'%' IDENTIFIED BY '$KUMA_DB_PASSWORD';
ALTER USER '$KUMA_DB_USER'@'%' IDENTIFIED BY '$KUMA_DB_PASSWORD';
GRANT SELECT ON \`$DB_NAME\`.* TO '$KUMA_DB_USER'@'%';
SQL
      note "User '$KUMA_DB_USER' created in '$DB_CONTAINER'."
    else
      note "Create this user on your database server, then enter its password below:"
      printf '\n      CREATE USER '\''%s'\''@'\''%%'\'' IDENTIFIED BY '\''…'\'';\n      GRANT SELECT ON `%s`.* TO '\''%s'\''@'\''%%'\'';\n\n' "$KUMA_DB_USER" "$DB_NAME" "$KUMA_DB_USER"
      KUMA_DB_PASSWORD="$(ask "Password of '$KUMA_DB_USER' (empty = use Kuma's own credentials, not recommended)" "")"
    fi
    ;;
  *)
    die "unknown database type '$DB_TYPE' in db-config.json"
    ;;
esac

# ------------------------------------------------------------------ settings
info "Settings"
HOST_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##' || echo UTC)"
TZ_VALUE="$(ask "Timezone for periods and schedules" "${HOST_TZ:-UTC}")"
KUMA_BASE_URL="$(ask "Public URL of Uptime Kuma, for links in reports (optional)" "${KUMA_URL_ARG:-$KUMA_BASE_URL}")"
if [ "$BASE_PATH" = "unset" ]; then
  BASE_PATH="$(ask "Serve the GUI under a sub-path of an existing site, e.g. /reports (empty = no)" "")"
fi
BASE_PATH="$(printf '%s' "$BASE_PATH" | sed 's#^/*#/#; s#/*$##')"; [ "$BASE_PATH" = "/" ] && BASE_PATH=""
BIND="$(ask "Publish the GUI on address (127.0.0.1 = local/reverse proxy only, 0.0.0.0 = everywhere)" "${BIND:-127.0.0.1}")"
PORT="$(ask "Port" "${PORT:-8080}")"
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$"; then
  warn "port $PORT already seems to be in use"
  PORT="$(ask "Port" "$((PORT + 1))")"
fi
ADMIN_PASSWORD="$(random_secret 20)"

# Small VPSes often run Kuma with little headroom. Kuma Reports needs ~70 MB idle and ~140 MB while
# rendering a PDF; a memory limit makes sure it can never push the kernel into killing Kuma itself.
MEM_TOTAL_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
MEM_AVAIL_MB="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
SWAP_MB="$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$MEM_TOTAL_MB" -gt 0 ] && [ "$MEM_TOTAL_MB" -lt 2048 ]; then MEM_LIMIT="256m"; else MEM_LIMIT="512m"; fi
note "Memory: ${MEM_AVAIL_MB} MB available of ${MEM_TOTAL_MB} MB, swap ${SWAP_MB} MB → container limit $MEM_LIMIT"
if [ "$MEM_AVAIL_MB" -gt 0 ] && [ "$MEM_AVAIL_MB" -lt 300 ]; then
  warn "only ${MEM_AVAIL_MB} MB memory available. Kuma Reports is limited to $MEM_LIMIT so it cannot starve Kuma,"
  warn "but PDF attachments may fail when memory runs out.$([ "$SWAP_MB" = 0 ] && echo ' Consider adding swap (e.g. a 1 GB swapfile).')"
fi

# ------------------------------------------------------------------ write files
info "Writing $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ "$BUILD_FROM_SOURCE" = 0 ]; then
  if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      warn "could not pull $IMAGE — using the local image"
    else
      warn "could not pull $IMAGE — building from source instead"
      BUILD_FROM_SOURCE=1
    fi
  fi
fi
if [ "$BUILD_FROM_SOURCE" = 1 ]; then
  if command -v git >/dev/null 2>&1; then
    [ -d src/.git ] || git clone -q --depth 1 "https://github.com/$REPO.git" src
  else
    mkdir -p src && curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz --strip-components=1 -C src
  fi
fi

umask 077
cat > .env <<EOF
# Kuma Reports — generated by install.sh on $(date -u +%Y-%m-%d). See .env.example in the repository.
KUMA_DB_TYPE=$DB_TYPE
KUMA_DB_USER=$KUMA_DB_USER
KUMA_DB_PASSWORD=$KUMA_DB_PASSWORD
KUMA_BASE_URL=$KUMA_BASE_URL
TZ=$TZ_VALUE
AUTH_MODE=form
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
IMPORT_INTERVAL=300
BASE_PATH=$BASE_PATH
EOF
umask 022

{
  echo "# Kuma Reports — generated by install.sh"
  echo "services:"
  echo "  kuma-reports:"
  if [ "$BUILD_FROM_SOURCE" = 1 ]; then
    echo "    build: ./src"
    echo "    image: kuma-reports:local"
  else
    echo "    image: $IMAGE"
  fi
  echo "    container_name: kuma-reports"
  echo "    restart: unless-stopped"
  echo "    mem_limit: $MEM_LIMIT"
  echo "    env_file: .env"
  echo "    ports:"
  echo "      - \"$BIND:$PORT:8080\""
  echo "    volumes:"
  if [ "$MOUNT_TYPE" = "volume" ]; then
    echo "      - kuma-data:/kuma:ro"
  else
    echo "      - $MOUNT_SOURCE:/kuma:ro"
  fi
  echo "      - ./data:/data"
  echo "    logging:"
  echo "      driver: json-file"
  echo "      options: { max-size: \"10m\", max-file: \"3\" }"
  if [ "${#EXTRA_NETWORKS[@]}" -gt 0 ]; then
    echo "    networks:"
    for n in "${EXTRA_NETWORKS[@]}"; do echo "      - $n"; done
  fi
  if [ "$MOUNT_TYPE" = "volume" ]; then
    echo ""
    echo "volumes:"
    echo "  kuma-data:"
    echo "    external: true"
    echo "    name: $MOUNT_NAME"
  fi
  if [ "${#EXTRA_NETWORKS[@]}" -gt 0 ]; then
    echo ""
    echo "networks:"
    for n in "${EXTRA_NETWORKS[@]}"; do printf '  %s:\n    external: true\n' "$n"; done
  fi
} > docker-compose.yml

# ------------------------------------------------------------------ start
info "Starting Kuma Reports"
if [ "$BUILD_FROM_SOURCE" = 1 ]; then docker compose up -d --build; else docker compose up -d; fi

printf '    waiting for it to become healthy '
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' kuma-reports 2>/dev/null || echo starting)"
  [ "$status" = "healthy" ] && break
  printf '.'; sleep 2
done
echo
if [ "${status:-}" != "healthy" ]; then
  warn "Kuma Reports did not report healthy yet. Check: docker compose -f $INSTALL_DIR/docker-compose.yml logs"
fi

URL_HOST="$BIND"; [ "$BIND" = "0.0.0.0" ] && URL_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

${G}${B}Kuma Reports is running.${N}

  URL       http://${URL_HOST:-localhost}:$PORT$BASE_PATH/
  Username  admin
  Password  $ADMIN_PASSWORD   ${D}(also in $INSTALL_DIR/.env)${N}

  Files     $INSTALL_DIR/docker-compose.yml, .env, data/
  Logs      docker compose -f $INSTALL_DIR/docker-compose.yml logs -f
  Upgrade   run this installer again

Next: open the GUI, check Mail (your Kuma e-mail notifications are listed there), and create a report.
EOF
if [ -n "$BASE_PATH" ]; then
  CLIENT_IP="${SSH_CLIENT%% *}"
  cat <<EOF
${B}Reverse proxy:${N} add this to the nginx ${B}server { … }${N} block of your Kuma site, then ${B}nginx -t && systemctl reload nginx${N}:

    location = $BASE_PATH { return 301 $BASE_PATH/; }
    location ^~ $BASE_PATH/ {
        allow ${CLIENT_IP:-203.0.113.10};   # $([ -n "$CLIENT_IP" ] && echo "your current IP (from this SSH session)" || echo "your IP")
        deny  all;
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

EOF
fi
if [ "$BIND" = "127.0.0.1" ]; then
  note "${D}The GUI is only reachable from this machine — put a reverse proxy in front or use an SSH tunnel:${N}"
  note "${D}ssh -L $PORT:127.0.0.1:$PORT $(whoami)@$(hostname -f 2>/dev/null || hostname)${N}"
fi
