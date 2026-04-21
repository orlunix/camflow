#!/usr/bin/env bash
# Build camflow into a deployable zipapp + wrapper.
#
# Output:
#   dist/camflow      — shell wrapper (finds Python 3.10+, execs the .pyz)
#   dist/camflow.pyz  — zipapp containing camflow package + vendored yaml
#
# Usage:
#   scripts/build.sh              # full build
#   scripts/build.sh --skip-tests # skip pytest
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
BUILD_DIR="$(mktemp -d)"
SKIP_TESTS=0

[[ "${1:-}" == "--skip-tests" ]] && SKIP_TESTS=1

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; }

cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Tests
# ---------------------------------------------------------------------------
if [[ $SKIP_TESTS -eq 0 ]]; then
    log "running tests ..."
    cd "$REPO_ROOT"
    python3 -m pytest tests/ -q --tb=short || { err "tests failed"; exit 1; }
fi

# ---------------------------------------------------------------------------
# 2. Version
# ---------------------------------------------------------------------------
VERSION=$(python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/src')
from camflow import __version__
print(__version__)
" 2>/dev/null || echo "0.1.0")
GIT_SHA=$(git -C "$REPO_ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=$(git -C "$REPO_ROOT" diff --quiet HEAD -- src/ 2>/dev/null && echo "" || echo "-dirty")
BUILD_STAMP="$GIT_SHA$GIT_DIRTY $(date '+%Y-%m-%d %H:%M')"
log "version: $VERSION ($BUILD_STAMP)"

# ---------------------------------------------------------------------------
# 3. Assemble zipapp contents
# ---------------------------------------------------------------------------
log "assembling zipapp ..."

# Copy camflow package
cp -r "$REPO_ROOT/src/camflow" "$BUILD_DIR/camflow"

# Vendor PyYAML into the build (just the yaml/ package)
YAML_SRC=$(python3 -c "import yaml, os; print(os.path.dirname(yaml.__file__))")
cp -r "$YAML_SRC" "$BUILD_DIR/yaml"
log "vendored yaml from $YAML_SRC"

# Write __main__.py entry point
cat > "$BUILD_DIR/__main__.py" << 'MAIN_EOF'
import sys
from camflow.cli_entry.main import main
sys.exit(main())
MAIN_EOF

# Stamp build info
python3 -c "
import pathlib
init = pathlib.Path('$BUILD_DIR/camflow/__init__.py')
text = init.read_text()
if '__build__' not in text:
    text += '\n__build__ = \"$BUILD_STAMP\"\n'
else:
    import re
    text = re.sub(r'__build__\s*=.*', '__build__ = \"$BUILD_STAMP\"', text)
init.write_text(text)
"

# ---------------------------------------------------------------------------
# 4. Build zipapp
# ---------------------------------------------------------------------------
mkdir -p "$DIST_DIR"
log "building zipapp ..."
python3 -m zipapp "$BUILD_DIR" \
    --output "$DIST_DIR/camflow.pyz"

# ---------------------------------------------------------------------------
# 5. Write shell wrapper
# ---------------------------------------------------------------------------
cat > "$DIST_DIR/camflow" << 'WRAPPER_EOF'
#!/bin/bash
# camflow — workflow engine for AI coding agents
# Auto-generated wrapper. Finds Python 3.10+ and runs camflow.pyz.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
PYZ="$SCRIPT_DIR/camflow.pyz"

if [[ ! -f "$PYZ" ]]; then
    # Also check ~/.cam/ (deployed location)
    PYZ="$HOME/.cam/camflow.pyz"
fi

if [[ ! -f "$PYZ" ]]; then
    echo "ERROR: camflow.pyz not found (checked $SCRIPT_DIR/ and ~/.cam/)" >&2
    exit 2
fi

# Find Python 3.10+ — try common locations
find_python() {
    for py in python3.12 python3.11 python3.10; do
        if command -v "$py" >/dev/null 2>&1; then
            echo "$py"; return
        fi
    done
    # Check if current python3 is 3.10+
    local ver
    ver=$(python3 -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null || echo 0)
    if [[ "$ver" -ge 310 ]]; then
        echo "python3"; return
    fi
    # Try sourcing bashrc (DC machines hide python in bashrc PATH)
    if [[ -f "$HOME/.bashrc" ]]; then
        eval "$(bash -l -c 'echo export PATH=\"$PATH\"' 2>/dev/null)" 2>/dev/null || true
        ver=$(python3 -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null || echo 0)
        if [[ "$ver" -ge 310 ]]; then
            echo "python3"; return
        fi
    fi
    return 1
}

PYTHON=$(find_python) || {
    echo "ERROR: Python 3.10+ required. Found: $(python3 --version 2>&1)" >&2
    exit 1
}

exec "$PYTHON" "$PYZ" "$@"
WRAPPER_EOF
chmod +x "$DIST_DIR/camflow"

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
log "verifying ..."
"$DIST_DIR/camflow" --help >/dev/null 2>&1 && ok "camflow $VERSION ($BUILD_STAMP)" || {
    python3 "$DIST_DIR/camflow.pyz" --help >/dev/null 2>&1 && ok "camflow $VERSION (pyz direct)" || {
        err "verification failed"
        exit 1
    }
}

PYZ_SIZE=$(du -h "$DIST_DIR/camflow.pyz" | cut -f1)
ok "built dist/camflow + dist/camflow.pyz ($PYZ_SIZE)"
