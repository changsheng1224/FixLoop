#!/bin/bash
set -e

# Prefer container env (SandboxManager sets HOME/PYTHONUSERBASE under /tmp).
# Do not clobber with /code/.local — that fought the manager's userbase path.
export HOME="${HOME:-/tmp/fixloop-home}"
export PYTHONUSERBASE="${PYTHONUSERBASE:-/tmp/fixloop-userbase}"
mkdir -p "$HOME" "$PYTHONUSERBASE" /tmp /code
cd /code

case "$1" in
    build)
        shift
        exec "$@"
        ;;
    test)
        shift
        exec "$@"
        ;;
    apply-patch)
        file="$2"
        diff_file="$3"
        cp "$file" "$file.bak.$(date +%s)"
        patch -p0 < "$diff_file"
        ;;
    revert-patch)
        file="$2"
        latest_backup=$(ls -t "$file.bak."* 2>/dev/null | head -1)
        if [ -n "$latest_backup" ]; then
            cp "$latest_backup" "$file"
        fi
        ;;
    *)
        echo "Usage: entrypoint.sh {build|test|apply-patch|revert-patch} [args...]"
        exit 1
        ;;
esac
