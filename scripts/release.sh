#!/usr/bin/env bash
# camflow release: build → deploy to all machines → verify.
#
# Mirrors camc's release flow. Ships two files per machine:
#   ~/.cam/camflow      — shell wrapper (finds Python 3.10+)
#   ~/.cam/camflow.pyz  — zipapp (camflow + vendored yaml)
#
# Usage:
#   scripts/release.sh                     # full release
#   scripts/release.sh --skip-tests        # skip pytest
#   scripts/release.sh --skip-build        # reuse existing dist/
#   scripts/release.sh --only NAME[,NAME]  # deploy to subset
#   scripts/release.sh --dry-run           # print plan, don't deploy
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MACHINES_FILE="${CAMC_MACHINES_FILE:-$HOME/.cam/machines.json}"
DIST_DIR="$REPO_ROOT/dist"
REMOTE_DIR="~/.cam"

SSH_TIMEOUT=10
SKIP_TESTS=0
SKIP_BUILD=0
DRY_RUN=0
ONLY=""

log()   { printf '\033[1;34m[release]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[release]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[release]\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[1;31m[release]\033[0m %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-tests) SKIP_TESTS=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --only)       ONLY="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            err "unknown flag: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# 1. Build
# ---------------------------------------------------------------------------
if [[ $SKIP_BUILD -eq 1 ]]; then
    [[ -f "$DIST_DIR/camflow.pyz" ]] || { err "--skip-build but dist/camflow.pyz missing"; exit 1; }
    log "skip build (using existing dist/)"
else
    BUILD_ARGS=()
    [[ $SKIP_TESTS -eq 1 ]] && BUILD_ARGS+=(--skip-tests)
    "$REPO_ROOT/scripts/build.sh" "${BUILD_ARGS[@]}"
fi

LOCAL_VERSION=$(python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT/src'); from camflow import __version__; print(__version__)" 2>/dev/null || echo "0.1.0")
log "local version: $LOCAL_VERSION"

# ---------------------------------------------------------------------------
# 2. Parse machines.json
# ---------------------------------------------------------------------------
if [[ ! -f "$MACHINES_FILE" ]]; then
    err "machines file not found: $MACHINES_FILE"
    exit 1
fi

# Parse JSON with python (available everywhere, no jq dependency)
MACHINES=$(python3 -c "
import json, sys
with open('$MACHINES_FILE') as f:
    machines = json.load(f)
for m in machines:
    name = m.get('name', '')
    host = m.get('host', '')
    user = m.get('user', '')
    port = str(m.get('port', '') or '')
    if host:
        print('\t'.join([name, host, user, port]))
")

# ---------------------------------------------------------------------------
# 3. Filter --only
# ---------------------------------------------------------------------------
if [[ -n "$ONLY" ]]; then
    FILTERED=""
    IFS=',' read -ra TARGETS <<< "$ONLY"
    while IFS=$'\t' read -r name host user port; do
        for t in "${TARGETS[@]}"; do
            [[ "$name" == "$t" ]] && FILTERED+="$name"$'\t'"$host"$'\t'"$user"$'\t'"$port"$'\n'
        done
    done <<< "$MACHINES"
    MACHINES="$FILTERED"
fi

# ---------------------------------------------------------------------------
# 4. Deploy
# ---------------------------------------------------------------------------
ssh_args() {
    local user="$1" host="$2" port="$3"
    local args=(-n -o ConnectTimeout=$SSH_TIMEOUT -o StrictHostKeyChecking=no
                -o ControlMaster=auto -o ControlPersist=600)
    [[ -n "$port" ]] && args+=(-p "$port")
    printf '%s\n' "${args[@]}"
}

scp_args() {
    ssh_args "$@" | sed -e '/^-n$/d' -e 's/^-p$/-P/'
}

deployed=0
verified=0
failed=0
failures=()

while IFS=$'\t' read -r name host user port; do
    [[ -z "$name" ]] && continue
    target="${user:+${user}@}${host}"
    label="$name ($target${port:+:$port})"
    log "→ $label"

    mapfile -t SSH_OPTS < <(ssh_args "$user" "$host" "$port")
    mapfile -t SCP_OPTS < <(scp_args "$user" "$host" "$port")

    if [[ $DRY_RUN -eq 1 ]]; then
        printf '   scp %s dist/camflow dist/camflow.pyz → %s:%s/\n' "${SCP_OPTS[*]}" "$target" "$REMOTE_DIR"
        printf '   ssh %s %s %s/camflow version\n' "${SSH_OPTS[*]}" "$target" "$REMOTE_DIR"
        continue
    fi

    # Ensure ~/.cam exists
    if ! ssh "${SSH_OPTS[@]}" "$target" "mkdir -p ~/.cam" >/dev/null 2>&1; then
        err "   mkdir ~/.cam failed on $label"
        failed=$((failed + 1)); failures+=("$name:mkdir"); continue
    fi

    # SCP both files
    if ! scp "${SCP_OPTS[@]}" "$DIST_DIR/camflow" "$DIST_DIR/camflow.pyz" "$target:$REMOTE_DIR/" </dev/null >/dev/null 2>&1; then
        err "   scp failed on $label"
        failed=$((failed + 1)); failures+=("$name:scp"); continue
    fi
    ssh "${SSH_OPTS[@]}" "$target" "chmod +x $REMOTE_DIR/camflow" >/dev/null 2>&1 || true
    deployed=$((deployed + 1))

    # Verify — check --help runs without error
    if ssh "${SSH_OPTS[@]}" "$target" "bash -l -c '$REMOTE_DIR/camflow --help'" >/dev/null 2>&1; then
        ok "   verified"
    else
        err "   verify failed on $label"
        failed=$((failed + 1)); failures+=("$name:verify"); continue
    fi
    verified=$((verified + 1))
done <<< "$MACHINES"

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo
printf '\033[1m%s\033[0m\n' "summary"
printf '   local version: %s\n' "$LOCAL_VERSION"
printf '   deployed     : %d\n' "$deployed"
printf '   verified     : %d\n' "$verified"
printf '   failed       : %d\n' "$failed"
if (( ${#failures[@]} > 0 )); then
    printf '   failed hosts : %s\n' "${failures[*]}"
    exit 1
fi
exit 0
