#!/usr/bin/env bash
# Runs each scenario against a fresh, throwaway state folder.
# The output is saved in sample-output.txt.
cd "$(dirname "$0")" || exit 1
export CHARTER_STATE_DIR="$(mktemp -d)"

show() {
  local shown="python3 charter.py" arg
  for arg in "$@"; do
    if [[ "$arg" == *" "* ]]; then shown+=" \"$arg\""; else shown+=" $arg"; fi
  done
  echo "\$ $shown"
  python3 charter.py "$@" 2>&1
  echo
}

echo "=== A timeout and a retry, a pause for approval, then the send ==="
echo
show run REQ-1042 --fail-availability 1
show run REQ-1042
show approve REQ-1042 --by dana
show status REQ-1042

echo "=== Retries run out, a rerun continues, then the draft is rejected ==="
echo
show run REQ-1043 --fail-availability 3
show run REQ-1043
show reject REQ-1043 --by dana --reason "Client wants a larger cabin"
