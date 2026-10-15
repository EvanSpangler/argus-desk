#!/usr/bin/env bash
# Gate 0 for argus-desk: can this workstation load the mod. Read-only except for one env line in
# ~/.claude/settings.json (added with --install, removed with --remove). Prints short lines only.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
say() { printf 'GATE0 %s\n' "$*"; }

if [ "${1:-}" = "--remove" ]; then
  python3 - "$SETTINGS" <<'PY'
import json, sys, os
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
env = d.get("env") or {}
env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
if env: d["env"] = env
else: d.pop("env", None)
json.dump(d, open(p, "w"), indent=2)
print("GATE0 removed CLAUDE_CODE_PLUGIN_DIRS from", p)
PY
  exit 0
fi

say "host $(hostname) user $(id -un) date $(date -u +%FT%TZ)"
say "claude $(claude --version 2>&1 | head -1)"
say "python3 $(python3 --version 2>&1 || echo MISSING)"
say "git $(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo not-a-checkout)"
if command -v argus >/dev/null 2>&1; then
  say "argus launcher $(command -v argus): $(grep -m1 -o 'claude[^|;&]*' "$(command -v argus)" 2>/dev/null | head -c 80)"
else
  say "argus launcher: not on PATH"
fi
V=$(cd "$HERE" && claude plugin validate . 2>&1)
say "validate $(echo "$V" | grep -o -E '[✔√✓].*|[✘×✗].*' | tail -1)"
echo "$V" | grep -i -E "policy|refus|not allowed|disabled" | head -3 | sed 's/^/GATE0 validate note: /'
T=$(cd "$HERE" && timeout 300 claude plugin test . 2>&1)
say "plugin test $(echo "$T" | grep -E '^ *[0-9]+ (pass|fail)' | tr '\n' ' ')"
echo "$T" | grep -E "^\(fail\)" | head -3 | sed 's/^/GATE0 /'
P=$(cd "$HERE/checks" && python3 -I -m unittest discover -s tests 2>&1 | tail -1)
say "python tests $P"
say "plugins enabled? $(claude plugin list 2>&1 | head -3 | tr '\n' ' ' | head -c 120)"
if [ -f "$SETTINGS" ]; then
  say "settings env: $(python3 -c "import json;d=json.load(open('$SETTINGS'));print(d.get('env',{}).get('CLAUDE_CODE_PLUGIN_DIRS','unset'))" 2>&1)"
else
  say "settings: $SETTINGS absent"
fi
if [ "${1:-}" = "--install" ]; then
  python3 - "$SETTINGS" "$HERE" <<'PY'
import json, sys, os
p, here = sys.argv[1], sys.argv[2]
d = json.load(open(p)) if os.path.exists(p) else {}
env = d.setdefault("env", {})
env["CLAUDE_CODE_PLUGIN_DIRS"] = here
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(d, open(p, "w"), indent=2)
print("GATE0 installed CLAUDE_CODE_PLUGIN_DIRS=%s in %s (restart Claude Code to load; undo: gate0.sh --remove)" % (here, p))
PY
fi
say "END"
