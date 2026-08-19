#!/usr/bin/env bash
# End-to-end smoke test against a running stack.
#
#   docker compose up -d --build
#   ./examples/demo.sh
#
# Override the address with GUI=..., and set TOKEN=... when GUI_AUTH_TOKEN is
# configured on the server.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -f "$HERE/sample-marked.png" ] || python3 "$HERE/make-binary-examples.py"

exec python3 "$HERE/demo.py" "$@"
