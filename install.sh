#!/usr/bin/env bash
# muse2api-vps-kit installer: czg86389-hub/muse2api on one small VPS.
# Ubuntu 22.04+ / Debian 12+, run as root. Idempotent: re-running keeps keys, accounts and data.
# Full option list: bash install.sh --help
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="https://github.com/czg86389-hub/muse2api.git"
DEFAULT_COMMIT="f7f5be5fce4d4e3a4f7240a09ac95c02ed60ff46"   # upstream v1.6.12, 2026-11-19

NAME="muse2api"
SVC_USER=""
PREFIX=""
STATE_DIR=""
ETC_DIR=""
PORT=18610
CDP_PORT=19210
REPO="$DEFAULT_REPO"
COMMIT="$DEFAULT_COMMIT"
CHROME=""
CONCURRENCY=""
MEMORY_HIGH=""
DOMAIN=""
HTTPS_PORT=443
MAX_BODY="60MB"
DO_SWAP=1
SWAP_MB=2048
DO_CADDY=1
DO_APT=1
LOGIN_TOOLS=1
METER=0
METER_QUOTA_EVERY=21600

usage() {
  cat <<'EOF'
Usage: sudo bash install.sh [options]

Core
  --name NAME            systemd unit name and default for user/paths (default: muse2api)
  --user USER            system user that runs muse2api and Chrome (default: NAME)
  --prefix DIR           code + venv (default: /opt/NAME)
  --state-dir DIR        data, Chrome profile, HOME of the user (default: /var/lib/NAME)
  --etc-dir DIR          env files with keys, 0600 root (default: /etc/NAME)
  --port N               loopback port of muse2api (default: 18610)
  --cdp-port N           Chrome DevTools port, loopback (default: 19210)
  --commit REF           upstream commit/tag to pin (default: v1.6.12 f7f5be5)
  --repo URL             upstream git URL (default: github.com/czg86389-hub/muse2api)
  --chrome PATH          use this Chrome/Chromium binary instead of installing one
  --concurrency N        MUSE2API_CONCURRENCY (default: 1 below 3 GB RAM, else 2)
  --memory-high SIZE     systemd MemoryHigh, e.g. 1400M (default: 70% of RAM)

HTTPS front (optional)
  --domain HOST          install Caddy, serve https://HOST with a separate front key
  --https-port N         public HTTPS port (default: 443; use another one if 443 is taken)
  --max-body SIZE        largest request body Caddy accepts (default: 60MB)
  --no-caddy             never touch Caddy, even with --domain

Other
  --no-swap              do not add a swap file (default: add 2G when RAM < 3 GB and no swap)
  --swap-mb N            swap file size in MB (default: 2048)
  --no-apt               do not install packages (you manage git, python3-venv, ffmpeg, Chrome)
  --no-login-tools       skip Playwright (needed only by login/muse_login.py)
  --meter                install the SQLite meter + 15-minute systemd timer
  --meter-quota-every S  live quota read at most every S seconds (default: 21600, minimum 3600)
  -h, --help             this text
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --user) SVC_USER="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --etc-dir) ETC_DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --cdp-port) CDP_PORT="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --chrome) CHROME="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --memory-high) MEMORY_HIGH="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; shift 2 ;;
    --max-body) MAX_BODY="$2"; shift 2 ;;
    --no-caddy) DO_CADDY=0; shift ;;
    --no-swap) DO_SWAP=0; shift ;;
    --swap-mb) SWAP_MB="$2"; shift 2 ;;
    --no-apt) DO_APT=0; shift ;;
    --no-login-tools) LOGIN_TOOLS=0; shift ;;
    --meter) METER=1; shift ;;
    --meter-quota-every) METER_QUOTA_EVERY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

SVC_USER="${SVC_USER:-$NAME}"
PREFIX="${PREFIX:-/opt/$NAME}"
STATE_DIR="${STATE_DIR:-/var/lib/$NAME}"
ETC_DIR="${ETC_DIR:-/etc/$NAME}"
APP_DIR="$PREFIX/app"
VENV="$PREFIX/venv"
ENV_FILE="$ETC_DIR/muse2api.env"
FRONT_ENV="$ETC_DIR/front.env"
UNIT_FILE="/etc/systemd/system/$NAME.service"
METER_DB_DIR="/var/lib/$NAME-meter"

[[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || die "--name must be lowercase letters, digits and dashes"
[[ "$PORT" =~ ^[0-9]+$ && "$CDP_PORT" =~ ^[0-9]+$ && "$HTTPS_PORT" =~ ^[0-9]+$ ]] || die "ports must be numbers"
for d in "$PREFIX" "$STATE_DIR" "$ETC_DIR"; do
  [[ "$d" == /* && "$d" != *"|"* && "$d" != *" "* ]] || die "paths must be absolute, without spaces or '|': $d"
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash install.sh ...)"
command -v systemctl >/dev/null || die "systemd is required"

# ---------------------------------------------------------------- facts about the box
MEM_MB=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))
[ -n "$CONCURRENCY" ] || { if [ "$MEM_MB" -lt 3072 ]; then CONCURRENCY=1; else CONCURRENCY=2; fi; }
[ -n "$MEMORY_HIGH" ] || MEMORY_HIGH="$(( MEM_MB * 70 / 100 ))M"
[ "$MEM_MB" -ge 1500 ] || warn "only ${MEM_MB} MB RAM: headless Chrome peaked near 870 MB on our 2 GB box; expect swapping or OOM"
# parse os-release instead of sourcing it: sourcing would overwrite $NAME (our unit name)
os_release_field() { sed -n "s/^$1=//p" /etc/os-release 2>/dev/null | head -n1 | tr -d "\"'"; }
OS_ID="$(os_release_field ID)"
OS_LIKE="$(os_release_field ID_LIKE)"
case " $OS_ID $OS_LIKE " in *" debian "*|*" ubuntu "*) ;; *) warn "untested distro ${OS_ID:-unknown}; continuing" ;; esac
CHANGED=0   # set to 1 when anything the service reads has changed -> restart

# ---------------------------------------------------------------- packages
apt_install_missing() {
  local missing=() p
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
  done
  [ ${#missing[@]} -eq 0 ] && return 0
  log "apt: installing ${missing[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
}

find_chrome() {
  local c
  for c in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  return 1
}

if [ "$DO_APT" -eq 1 ]; then
  apt_install_missing ca-certificates curl git python3 python3-venv ffmpeg fonts-wqy-zenhei
fi
command -v git >/dev/null || die "git missing (drop --no-apt or install it)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 >= 3.10 required"

if [ -z "$CHROME" ]; then
  if ! CHROME="$(find_chrome)"; then
    [ "$DO_APT" -eq 1 ] || die "no Chrome/Chromium found; pass --chrome PATH or drop --no-apt"
    ARCH="$(dpkg --print-architecture)"
    if [ "$ARCH" = "amd64" ]; then
      log "installing Google Chrome stable (official .deb from dl.google.com)"
      TMP_DEB="$(mktemp -d)"
      curl -fsSL -o "$TMP_DEB/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$TMP_DEB/chrome.deb"
      rm -rf "$TMP_DEB"
    elif [ "$OS_ID" = "debian" ]; then
      apt_install_missing chromium
    else
      die "no Google Chrome build for $ARCH; Ubuntu's chromium is a snap (profile paths break). Install a Chromium and pass --chrome PATH"
    fi
    CHROME="$(find_chrome)" || die "Chrome install did not produce a binary"
  fi
fi
case "$CHROME" in /snap/*) warn "snap Chromium can only write under \$HOME; keep --state-dir as the user's HOME (default)";; esac
[ -x "$CHROME" ] || die "not executable: $CHROME"
log "browser: $CHROME ($("$CHROME" --version 2>/dev/null || echo unknown version))"

# ---------------------------------------------------------------- user and directories
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  log "creating system user $SVC_USER"
  useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
SVC_UID="$(id -u "$SVC_USER")"
for p in "$PORT" "$CDP_PORT"; do
  # A second instance on the same CDP port would silently attach to the first one's Chrome.
  owner="$(ss -Hltne "sport = :$p" 2>/dev/null | grep -o 'uid:[0-9]*' | head -n 1 | cut -d: -f2 || true)"
  if [ -n "$(ss -Hltn "sport = :$p" 2>/dev/null)" ] && [ "$owner" != "$SVC_UID" ]; then
    die "port $p is already used by another process (uid ${owner:-0}); choose --port / --cdp-port"
  fi
done
install -d -m 0755 -o root -g root "$PREFIX"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$STATE_DIR"
install -d -m 0700 -o root -g root "$ETC_DIR"

# ---------------------------------------------------------------- upstream code, pinned
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" rev-parse --verify --quiet "$COMMIT^{commit}" >/dev/null \
    || git -C "$APP_DIR" fetch --quiet --tags origin
else
  log "cloning $REPO"
  git clone --quiet "$REPO" "$APP_DIR"
fi
TARGET="$(git -C "$APP_DIR" rev-parse --verify --quiet "$COMMIT^{commit}")" || die "commit $COMMIT not found in $REPO"
if [ "$(git -C "$APP_DIR" rev-parse HEAD)" != "$TARGET" ]; then
  [ -z "$(git -C "$APP_DIR" status --porcelain)" ] || die "$APP_DIR has local changes; refusing to switch commits"
  git -C "$APP_DIR" -c advice.detachedHead=false checkout --quiet --detach "$TARGET"
  CHANGED=1
fi
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
log "upstream pinned at $(git -C "$APP_DIR" rev-parse --short HEAD) ($(git -C "$APP_DIR" log -1 --format=%s | cut -c1-60))"

# kit tools live next to the code so units and docs have stable paths
install -d -m 0755 "$PREFIX/kit" "$PREFIX/kit/login" "$PREFIX/kit/meter"
install -m 0755 "$KIT_DIR/smoke.py" "$PREFIX/kit/smoke.py"
install -m 0755 "$KIT_DIR/login/muse_login.py" "$PREFIX/kit/login/muse_login.py"
install -m 0755 "$KIT_DIR/meter/muse_meter.py" "$PREFIX/kit/meter/muse_meter.py"

# ---------------------------------------------------------------- python venv
if [ ! -x "$VENV/bin/python" ]; then
  log "creating venv $VENV"
  python3 -m venv "$VENV"
  CHANGED=1
fi
REQ_SUM="$(sha256sum "$APP_DIR/requirements.txt" | cut -d' ' -f1)$LOGIN_TOOLS"
if [ "$(cat "$VENV/.kit-req" 2>/dev/null)" != "$REQ_SUM" ]; then
  log "pip: installing upstream requirements"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
  if [ "$LOGIN_TOOLS" -eq 1 ]; then
    log "pip: installing playwright (drives the system Chrome; no browser download)"
    "$VENV/bin/pip" install --quiet --disable-pip-version-check "playwright>=1.45"
  fi
  echo "$REQ_SUM" > "$VENV/.kit-req"
  CHANGED=1
fi

# ---------------------------------------------------------------- keys and env file
gen_key() { python3 -c 'import secrets, sys; print(sys.argv[1] + secrets.token_hex(24))' "$1"; }
read_env() { [ -f "$2" ] && sed -n "s/^$1=//p" "$2" | tail -n 1 || true; }

KEY="$(read_env MUSE2API_KEY "$ENV_FILE")"
[ -n "$KEY" ] || KEY="$(gen_key m2a_)"
PUBLIC_BASE=""
if [ -n "$DOMAIN" ]; then
  if [ "$HTTPS_PORT" = 443 ]; then PUBLIC_BASE="https://$DOMAIN"; else PUBLIC_BASE="https://$DOMAIN:$HTTPS_PORT"; fi
fi
MARKER="# --- lines below this marker are yours; install.sh keeps them ---"
umask 077
NEW_ENV="$(mktemp "$ETC_DIR/.env.XXXXXX")"
{
  echo "# muse2api settings written by muse2api-vps-kit install.sh. Secret: never commit or paste."
  echo "MUSE2API_KEY=$KEY"
  echo "MUSE2API_HOST=127.0.0.1"
  echo "MUSE2API_PORT=$PORT"
  echo "MUSE2API_HOME=$STATE_DIR"
  echo "MUSE2API_HOME_DIR=$STATE_DIR"
  echo "MUSE2API_PROFILE_ROOT=$STATE_DIR/muse2api-profiles"
  echo "MUSE2API_CHROMIUM=$CHROME"
  echo "MUSE2API_CDP_PORT=$CDP_PORT"
  echo "MUSE2API_CONCURRENCY=$CONCURRENCY"
  echo "MUSE2API_PUBLIC_BASE=$PUBLIC_BASE"
  echo "# pinned deployment: the admin page's one-click upgrade refuses to run"
  echo "MUSE2API_MANAGED_DEPLOYMENT=1"
  echo "$MARKER"
  if [ -f "$ENV_FILE" ] && grep -qxF "$MARKER" "$ENV_FILE"; then
    sed -n "\|^$MARKER\$|,\$p" "$ENV_FILE" | tail -n +2
  fi
} > "$NEW_ENV"
umask 022
if ! cmp -s "$NEW_ENV" "$ENV_FILE" 2>/dev/null; then
  mv -f "$NEW_ENV" "$ENV_FILE"
  CHANGED=1
else
  rm -f "$NEW_ENV"
fi
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# ---------------------------------------------------------------- swap
if [ "$DO_SWAP" -eq 1 ] && [ "$MEM_MB" -lt 3072 ]; then
  if [ -n "$(swapon --noheadings --show 2>/dev/null)" ]; then
    log "swap already active; leaving it alone"
  elif [ -e /swapfile ]; then
    warn "/swapfile exists but is not active; not touching it"
  else
    log "adding ${SWAP_MB} MB swap at /swapfile (RAM is ${MEM_MB} MB)"
    fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -qE '^/swapfile[[:space:]]' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

# ---------------------------------------------------------------- systemd unit
render() {  # render TEMPLATE > stdout, replacing @VARS@
  sed -e "s|@NAME@|$NAME|g" -e "s|@USER@|$SVC_USER|g" -e "s|@PREFIX@|$PREFIX|g" \
      -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@ETC_DIR@|$ETC_DIR|g" -e "s|@PORT@|$PORT|g" \
      -e "s|@MEMORY_HIGH@|$MEMORY_HIGH|g" -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@HTTPS_PORT@|$HTTPS_PORT|g" \
      -e "s|@MAX_BODY@|$MAX_BODY|g" -e "s|@METER_DB_DIR@|$METER_DB_DIR|g" \
      -e "s|@METER_QUOTA_EVERY@|$METER_QUOTA_EVERY|g" "$1"
}
install_rendered() {  # install_rendered TEMPLATE DEST MODE -> returns 0 if changed
  local tmp; tmp="$(mktemp)"
  render "$1" > "$tmp"
  if cmp -s "$tmp" "$2" 2>/dev/null; then rm -f "$tmp"; return 1; fi
  install -m "$3" "$tmp" "$2"; rm -f "$tmp"; return 0
}
if install_rendered "$KIT_DIR/systemd/muse2api.service.tmpl" "$UNIT_FILE" 0644; then CHANGED=1; fi
systemctl daemon-reload
systemctl enable --quiet "$NAME.service"
if [ "$CHANGED" -eq 1 ] || ! systemctl is-active --quiet "$NAME.service"; then
  log "starting $NAME.service"
  systemctl restart "$NAME.service"
fi

# ---------------------------------------------------------------- local checks
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
log "waiting for http://127.0.0.1:$PORT/healthz"
for _ in $(seq 1 45); do
  [ "$(http_code "http://127.0.0.1:$PORT/healthz")" = 200 ] && break
  sleep 1
done
HEALTH="$(http_code "http://127.0.0.1:$PORT/healthz")"
AUTHED="$(printf 'Authorization: Bearer %s\n' "$KEY" | http_code -H @- "http://127.0.0.1:$PORT/v1/models")"
NOKEY="$(http_code "http://127.0.0.1:$PORT/v1/models")"
log "healthz=$HEALTH  /v1/models with key=$AUTHED  without key=$NOKEY"
[ "$HEALTH" = 200 ] && [ "$AUTHED" = 200 ] && [ "$NOKEY" = 401 ] \
  || die "service checks failed; see: journalctl -u $NAME -n 50 --no-pager"

# Chrome only starts on the first real request, so probe it now under the same user and sandbox.
PROBE_DIR="$STATE_DIR/kit-chrome-probe"
if PROBE_OUT="$(systemd-run --quiet --wait --pipe --collect \
      -p User="$SVC_USER" -p Group="$SVC_USER" -p Environment=HOME="$STATE_DIR" \
      -p WorkingDirectory="$STATE_DIR" -p NoNewPrivileges=yes -p PrivateTmp=yes \
      -p ProtectSystem=full -p ProtectHome=yes -p MemoryHigh="$MEMORY_HIGH" -p RuntimeMaxSec=90 \
      "$CHROME" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
      --user-data-dir="$PROBE_DIR" --dump-dom 'data:text/html,<p>kit-probe-ok</p>' 2>/dev/null)" \
   && grep -q 'kit-probe-ok' <<<"$PROBE_OUT"; then
  log "Chrome starts as $SVC_USER under the service sandbox"
else
  warn "Chrome probe failed; generation will fail until this works. Try: sudo -u $SVC_USER HOME=$STATE_DIR $CHROME --headless=new --no-sandbox --dump-dom about:blank"
fi
rm -rf "$PROBE_DIR"

# ---------------------------------------------------------------- HTTPS front (Caddy)
if [ -n "$DOMAIN" ] && [ "$DO_CADDY" -eq 1 ]; then
  if ! command -v caddy >/dev/null; then
    if apt-cache policy caddy 2>/dev/null | grep -q 'Candidate: [0-9]'; then
      apt_install_missing caddy
    else
      log "adding the official Caddy apt repository"
      apt_install_missing gnupg debian-keyring debian-archive-keyring apt-transport-https
      curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        > /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
    fi
  fi
  for p in 80 "$HTTPS_PORT"; do
    holder="$(ss -Hltnp "sport = :$p" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -n 1 | cut -d'"' -f2 || true)"
    [ -z "$holder" ] || [ "$holder" = caddy ] || die "port $p is used by $holder; pick --https-port (port 80 must stay free for the ACME HTTP-01 challenge)"
  done
  FRONT="$(read_env MUSE_FRONT_KEY "$FRONT_ENV")"
  [ -n "$FRONT" ] || FRONT="$(gen_key mfk_)"
  umask 077
  printf '# front key for HTTPS clients (Caddy swaps it for MUSE2API_KEY). Secret.\nMUSE_FRONT_KEY=%s\n' "$FRONT" > "$FRONT_ENV"
  umask 022
  chmod 0600 "$FRONT_ENV"
  install -d -m 0755 /etc/caddy/conf.d /etc/systemd/system/caddy.service.d
  CADDY_CHANGED=0
  if install_rendered "$KIT_DIR/caddy/muse2api.caddy.tmpl" "/etc/caddy/conf.d/$NAME.caddy" 0644; then CADDY_CHANGED=1; fi
  if install_rendered "$KIT_DIR/systemd/caddy-front.conf.tmpl" "/etc/systemd/system/caddy.service.d/$NAME-front.conf" 0644; then CADDY_CHANGED=1; fi
  CADDYFILE=/etc/caddy/Caddyfile
  IMPORT_LINE="import /etc/caddy/conf.d/*.caddy"
  if [ -f "$CADDYFILE" ] && grep -qxF "$IMPORT_LINE" "$CADDYFILE"; then
    :
  elif [ ! -s "$CADDYFILE" ] || grep -q 'root \* /usr/share/caddy' "$CADDYFILE"; then
    [ -f "$CADDYFILE" ] && [ ! -f "$CADDYFILE.dist-orig" ] && cp -p "$CADDYFILE" "$CADDYFILE.dist-orig"
    printf '# managed by muse2api-vps-kit; the stock file is in Caddyfile.dist-orig\n%s\n' "$IMPORT_LINE" > "$CADDYFILE"
    CADDY_CHANGED=1
  else
    warn "$CADDYFILE has your own sites; add this line to it, then: systemctl restart caddy"
    warn "    $IMPORT_LINE"
    CADDY_CHANGED=0
  fi
  # validate with both env files loaded, in a subshell, so the keys never hit argv or the screen
  # shellcheck disable=SC1090
  ( set -a; . "$ENV_FILE"; . "$FRONT_ENV"; set +a
    caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 ) \
    || die "Caddy config does not validate: caddy validate --config $CADDYFILE"
  systemctl daemon-reload
  systemctl enable --quiet caddy
  if [ "$CADDY_CHANGED" -eq 1 ] || [ "$CHANGED" -eq 1 ]; then systemctl restart caddy; fi
  log "waiting up to 90s for https://$DOMAIN:$HTTPS_PORT (DNS A record must point here, port 80 open for ACME)"
  FRONT_OK=""
  for _ in $(seq 1 30); do
    FRONT_OK="$(printf 'Authorization: Bearer %s\n' "$FRONT" | http_code -H @- "$PUBLIC_BASE/v1/models")"
    [ "$FRONT_OK" = 200 ] && break
    sleep 3
  done
  FRONT_NOKEY="$(http_code "$PUBLIC_BASE/v1/models")"
  FRONT_ADMIN="$(http_code "$PUBLIC_BASE/admin")"
  log "front: /v1/models with front key=$FRONT_OK  without=$FRONT_NOKEY  /admin=$FRONT_ADMIN"
  [ "$FRONT_OK" = 200 ] || warn "front not reachable yet (DNS or certificate); re-check later with the command in the README"
fi

# ---------------------------------------------------------------- meter (optional)
if [ "$METER" -eq 1 ]; then
  [ "$METER_QUOTA_EVERY" -eq 0 ] || [ "$METER_QUOTA_EVERY" -ge 3600 ] || die "--meter-quota-every must be 0 or >= 3600"
  install -d -m 0700 -o root -g root "$METER_DB_DIR"
  install_rendered "$KIT_DIR/meter/muse-meter.service.tmpl" "/etc/systemd/system/$NAME-meter.service" 0644 || true
  install_rendered "$KIT_DIR/meter/muse-meter.timer.tmpl" "/etc/systemd/system/$NAME-meter.timer" 0644 || true
  systemctl daemon-reload
  systemctl enable --quiet --now "$NAME-meter.timer"
  log "meter: $NAME-meter.timer every 15 min -> $METER_DB_DIR/meter.sqlite"
fi

# ---------------------------------------------------------------- next steps
cat <<EOF

muse2api is running: unit $NAME.service, user $SVC_USER, http://127.0.0.1:$PORT (loopback only)
Upstream code:  $APP_DIR (pinned $(git -C "$APP_DIR" rev-parse --short HEAD); the in-app upgrade button is disabled)
Keys are NOT printed. They live in root-only files:
  $ENV_FILE   MUSE2API_KEY   (admin + API key; never leaves this box)
EOF
[ -n "$DOMAIN" ] && [ "$DO_CADDY" -eq 1 ] && cat <<EOF
  $FRONT_ENV      MUSE_FRONT_KEY (give this one to clients of $PUBLIC_BASE/v1)
EOF
cat <<EOF

Next:
  1. Log a Muse account in from this box's own IP (you type the emailed code):
       sudo $VENV/bin/python $PREFIX/kit/login/muse_login.py --email you@example.com --key-file $ENV_FILE --api http://127.0.0.1:$PORT
  2. Smoke test (chat, image, text-to-video, image-to-video; uses a little quota):
       sudo python3 $PREFIX/kit/smoke.py --key-file $ENV_FILE --base http://127.0.0.1:$PORT
  3. Admin page, from your laptop:  ssh -L $PORT:127.0.0.1:$PORT root@<this-vps>
       open http://127.0.0.1:$PORT/admin and paste the key into the page (do not put ?key= in the URL)
EOF
