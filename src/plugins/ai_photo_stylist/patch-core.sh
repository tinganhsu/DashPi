#!/usr/bin/env bash
set -e

if [[ -z "${PROJECT_DIR:-}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
fi

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LOCK_DIR="${TMPDIR:-/tmp}/dashpi-ai-photo-stylist-core-patch.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "[INFO] Core patch already running, skipping."
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "[INFO] Patching core files for plugin blueprint support..."

PROJECT_DIR="$PROJECT_DIR" python3 -c "
import os
import sys

project_dir = os.environ['PROJECT_DIR']
plugin_dir = '$PLUGIN_DIR'
sys.path.insert(0, os.path.join(project_dir, 'src'))
sys.path.insert(0, plugin_dir)

from patch_core import patch_core_files

success, error = patch_core_files()
if not success:
    print(f'[ERROR] Patch failed: {error}')
    sys.exit(1)
print('[INFO] Core files patched successfully')
"

SERVICE_NAME="${APPNAME:-dashpi}"
if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    echo "[INFO] Restarting ${SERVICE_NAME} service."
    sudo systemctl restart "${SERVICE_NAME}.service" 2>&1 || echo "[WARN] Service restart failed"
else
    echo "[INFO] Service ${SERVICE_NAME}.service not found, skipping restart."
fi
