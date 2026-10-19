#!/usr/bin/env bash
# Preflight is read-only; run needs separate author approval and --confirm.
set -euo pipefail
launcher_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [[ -f "$launcher_dir/conference/journal_extension/amd_oneshot/cli.py" ]]; then
    package_root="$launcher_dir"
else
    package_root="$(CDPATH= cd -- "$launcher_dir/../../.." && pwd)"
fi
exec python3 -I "$package_root/conference/journal_extension/amd_oneshot/entrypoint.py" "$@"
