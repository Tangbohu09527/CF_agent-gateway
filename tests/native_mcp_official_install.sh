#!/usr/bin/env bash
# CI-only: use the fixed source installer's dependency stage and PM's full [all].
set -Eeuo pipefail
set +x
umask 077
[ "${GITHUB_ACTIONS:-}" = true ] || exit 2
SOURCE="$(realpath "${1:?official checkout}")"
SUMMARY="$(realpath -m "${2:?sanitized dependency summary}")"
AUDIT="$(realpath "$(dirname "${BASH_SOURCE[0]}")/native_mcp_dependencies.py")"
[ "$(git -C "$SOURCE" rev-parse HEAD)" = 427d605d4985c2d49a795493ef6c5d66413057d1 ]
[ -z "$(git -C "$SOURCE" status --porcelain)" ]
export HOME="$RUNNER_TEMP/native-official-user"
export HERMES_HOME="$(dirname "$SOURCE")"
export XDG_CACHE_HOME="$HOME/.cache"
export UV_CACHE_DIR="$XDG_CACHE_HOME/uv"
mkdir -p "$HOME" "$(dirname "$SUMMARY")"
set +e
bash "$SOURCE/scripts/install.sh" --dir "$SOURCE" --hermes-home "$HERMES_HOME" \
  --stage python-deps --non-interactive --skip-browser --skip-computer-use
INSTALL_RESULT=$?
set -e
if [ "$INSTALL_RESULT" -ne 0 ]; then
  python -I -B -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"installer":"official scripts/install.sh", "installer_stage":"python-deps", "installer_exit":int(sys.argv[2]), "all_requirements_satisfied":False})+"\n")' "$SUMMARY" "$INSTALL_RESULT"
  exit "$INSTALL_RESULT"
fi
RUNTIME="$(cd "$SOURCE" && python -B -c 'from pathlib import Path; from pm.environments import project_python; print(project_python(Path.cwd()))')"
"$RUNTIME" -I -B "$AUDIT" --hermes-source "$SOURCE" --output "$SUMMARY"
printf 'NATIVE_OFFICIAL_PYTHON=%s\n' "$RUNTIME" >> "$GITHUB_ENV"
