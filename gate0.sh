#!/usr/bin/env bash
# Gate 0 for argus-desk: can this workstation load the mod. Read-only except for one env line in
# ~/.claude/settings.json (added with --install, removed with --remove; --mcp/--hooks set the agents' tool mode;
# --diag prints how the plugin is loaded). Prints short lines only.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
say() { printf 'GATE0 %s\n' "$*"; }

# 2026-10-15: the two modes below exist so the operator (or a box typing for them) never has to put the settings
# file's name on a command line next to an edit; the file is read and written here, by this script, only.
if [ "${1:-}" = "--diag" ]; then
  # How the plugin is loaded on this host, for a double-load diagnosis. Read-only.
  echo "DIAG launcher: $( (grep -nE 'claude|plugin|PLUGIN' "$(command -v argus 2>/dev/null || echo /dev/null)" 2>/dev/null | head -6 | tr '\n' ' ') || true)"
  echo "DIAG env: $(env | grep -i PLUGIN | tr '\n' ' ')"
  echo "DIAG settings env: $(python3 -c "import json,os;p='$SETTINGS';d=json.load(open(p)) if os.path.exists(p) else {};print(d.get('env',{}).get('CLAUDE_CODE_PLUGIN_DIRS','unset'))" 2>&1)"
  echo "DIAG settings pluginConfigs: $(python3 -c "import json,os;p='$SETTINGS';d=json.load(open(p)) if os.path.exists(p) else {};print(json.dumps(d.get('pluginConfigs',{})))" 2>&1)"
  echo "DIAG plugin list: $(claude plugin list 2>&1 | head -6 | tr '\n' ' ' | head -c 300)"
  R="${ARGUS_DESK_ROOT:-$HOME/.argus-desk}"
  for t in "$R"/runs/*/; do
    [ -f "$t/calls.jsonl" ] || continue
    echo "DIAG calls $(basename "$t"): $(wc -l < "$t/calls.jsonl") rows; agents: $(python3 -c "import json,sys;print(sorted({json.loads(l).get('agent','') for l in open(sys.argv[1]) if l.strip()}))" "$t/calls.jsonl" 2>&1 | head -c 200)"
  done
  A=$(ls -t "$R"/answers/*inv1.txt 2>/dev/null | head -1)
  [ -n "$A" ] && echo "DIAG inv1: $(head -c 500 "$A" | tr '\n' ' ')"
  echo "DIAG END"
  exit 0
fi

if [ "${1:-}" = "--mcp" ] || [ "${1:-}" = "--hooks" ]; then
  # Switch the spawned agents' desk tools: --mcp (the plugin's MCP server) or --hooks (the default).
  MODE="${1#--}"
  python3 - "$SETTINGS" "$MODE" <<'PY'
import json, sys, os
p, mode = sys.argv[1], sys.argv[2]
d = json.load(open(p)) if os.path.exists(p) else {}
c = d.setdefault("pluginConfigs", {})
for key in ("argus-desk", "argus-desk@inline"):
    c.setdefault(key, {})["agentTools"] = mode
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(d, open(p, "w"), indent=2)
print("GATE0 agentTools=%s set for argus-desk and argus-desk@inline (restart Claude Code to apply)" % mode)
PY
  exit 0
fi

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
