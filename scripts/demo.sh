#!/usr/bin/env bash
# End-to-end AgentSafe demo. Everything happens inside a fresh temporary directory:
# both the demo project and the AgentSafe storage (AGENTSAFE_HOME) live there, and
# the directory is deleted on exit. Your real projects and ~/.agentsafe are untouched.
set -euo pipefail

AGENTSAFE="${AGENTSAFE:-agentsafe}"
DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agentsafe-demo.XXXXXX")"
trap 'rm -rf "$DEMO_DIR"' EXIT
export AGENTSAFE_HOME="$DEMO_DIR/agentsafe-home"
PROJECT="$DEMO_DIR/project"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; "$@"; }

step "Create a small Git project with committed, uncommitted and untracked files"
mkdir -p "$PROJECT/src"
cd "$PROJECT"
printf '# Calculator\n' > README.md
printf 'def add(a, b):\n    return a + b\n' > src/calc.py
printf '*.log\n' > .gitignore
git init -q
git -c user.name=demo -c user.email=demo@example.com add .
git -c user.name=demo -c user.email=demo@example.com commit -q -m "initial"
printf 'def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n' > src/calc.py
printf 'remember to add tests\n' > NOTES.md      # untracked
printf 'noise\n' > debug.log                     # git-ignored: not captured

step "Initialize AgentSafe and take a snapshot before the agent runs"
run "$AGENTSAFE" init
run "$AGENTSAFE" snapshot -m "before agent run"
SNAP="$("$AGENTSAFE" history | awk 'NR==2 {print $1}')"

step "Simulate an AI agent: edit a file, delete a file, create a file"
printf 'def add(a, b):\n    return a - b  # oops\n' > src/calc.py
rm NOTES.md
printf 'print("generated")\n' > src/generated.py

step "Inspect what changed"
run "$AGENTSAFE" history
run "$AGENTSAFE" diff "$SNAP"
run "$AGENTSAFE" verify "$SNAP" || echo "(verify exits 1 because files differ)"

step "Preview the restore (dry run changes nothing)"
run "$AGENTSAFE" restore "$SNAP" --dry-run

step "A plain restore is refused: the agent's changes are not saved anywhere yet"
run "$AGENTSAFE" restore "$SNAP" --yes || echo "(refused with exit code $?)"

step "Restore, explicitly resolving conflicts (agent's versions go to a backup snapshot)"
run "$AGENTSAFE" restore "$SNAP" --overwrite-conflicts --yes

step "Verify the project matches the snapshot again"
run "$AGENTSAFE" verify "$SNAP"
run cat src/calc.py NOTES.md
run test ! -e src/generated.py
run cat debug.log
run "$AGENTSAFE" history

step "Demo finished successfully; removing $DEMO_DIR"
