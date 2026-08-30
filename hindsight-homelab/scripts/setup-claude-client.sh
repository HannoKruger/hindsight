#!/usr/bin/env bash
# Point a machine's Claude Code at Hindsight, and switch off the memory it ships with.
#
# Claude Code has its own memory store. Left enabled it competes with Hindsight —
# the model uses whichever the prompt mentions and facts split across both — so this
# disables it rather than layering a second system on top.
#
# Everything here is user-scope, so it applies to every repo on the machine. Nothing
# crosses machines: run this once per host.
#
# Usage:  HINDSIGHT_TOKEN=hs_... ./setup-claude-client.sh [bank_id] [api_url]
set -euo pipefail

BANK="${1:-hanno}"
API="${2:-https://hindsight-api.local.hannokruger.com}"
TOKEN="${HINDSIGHT_TOKEN:?set HINDSIGHT_TOKEN to the API key for this bank}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

# Fail before writing anything if the API is unreachable or the key is wrong —
# a config that points somewhere dead is worse than no config, because recall
# degrades silently to "no memories" rather than erroring.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  -H "Authorization: Bearer $TOKEN" "$API/v1/default/banks/$BANK/documents" || echo 000)
[ "$code" = "200" ] || { echo "cannot reach $API bank $BANK (HTTP $code)" >&2; exit 1; }

mkdir -p "$HOME/.hindsight" "$CLAUDE_DIR"
umask 077
python3 - "$API" "$TOKEN" "$BANK" <<'PY'
import json, os, sys
api, token, bank = sys.argv[1:4]
p = os.path.expanduser("~/.hindsight/claude-code.json")
cfg = {}
if os.path.exists(p):
    try: cfg = json.load(open(p))
    except ValueError: pass
cfg.update({"hindsightApiUrl": api, "hindsightApiToken": token, "bankId": bank,
            "autoRecall": True, "autoRetain": True,
            "retainEveryNTurns": 25, "retainMaxChars": 40000})
json.dump(cfg, open(p, "w"), indent=2)
os.chmod(p, 0o600)
print(f"  ~/.hindsight/claude-code.json -> bank {bank}")
PY

python3 - "$CLAUDE_DIR" <<'PY'
import json, os, sys
p = os.path.join(sys.argv[1], "settings.json")
d = {}
if os.path.exists(p):
    try: d = json.load(open(p))
    except ValueError:
        print("  settings.json is not valid JSON — refusing to overwrite", file=sys.stderr); raise SystemExit(1)
d.setdefault("extraKnownMarketplaces", {})["hindsight"] = {
    "source": {"source": "github", "repo": "HannoKruger/hindsight"}}
d.setdefault("enabledPlugins", {})["hindsight-memory@hindsight"] = True
d.setdefault("env", {})["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
perms = d.setdefault("permissions", {})
allow, deny = perms.setdefault("allow", []), perms.setdefault("deny", [])
plugin = "mcp__plugin_hindsight-memory_hindsight__agent_knowledge_"
conn = "mcp__claude_ai_Hindsight__"
# Both tool families: which one a session gets varies, and a missing allow rule
# fails the save silently in non-interactive runs.
for t in ["ingest","ingest_file","recall","get_current_bank","list_pages","get_page","create_page","update_page"]:
    if plugin+t not in allow: allow.append(plugin+t)
for t in ["retain","sync_retain","recall","reflect","search_knowledge_base","list_tags"]:
    if conn+t not in allow: allow.append(conn+t)
for t in [plugin+"delete_page", conn+"delete_bank", conn+"clear_memories"]:
    if t not in deny: deny.append(t)
json.dump(d, open(p, "w"), indent=2)
print("  settings.json -> plugin enabled, built-in memory disabled, tools allowed")
PY

MD="$CLAUDE_DIR/CLAUDE.md"
if ! grep -q "Long-term memory is \*\*Hindsight\*\*" "$MD" 2>/dev/null; then
  cat >> "$MD" <<MDEOF

# Memory

Long-term memory is **Hindsight** (bank \`$BANK\`). It is the only memory system here.

- **Recall is automatic.** A \`UserPromptSubmit\` hook injects relevant memories as
  \`<hindsight_memories>\` before every prompt. They are already in context — read them; do
  not go looking for a memory tool to fetch them, and never claim you have no memory.
- **Retention is automatic.** A \`Stop\` hook saves the transcript for fact extraction.
- **To save explicitly**, use whichever Hindsight tool the session has — availability varies.
  Prefer \`mcp__claude_ai_Hindsight__sync_retain\`; if the claude.ai connector is not loaded,
  use \`mcp__plugin_hindsight-memory_hindsight__agent_knowledge_ingest\`. Same bank either way.
- **Claude Code's own memory is switched off** (\`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1\`), so
  there is no built-in memory tool or auto-injected index. Do not look for one.
- **You cannot delete memory.** Correct a wrong memory by saving a corrected version.
MDEOF
  echo "  CLAUDE.md -> memory section appended"
else
  echo "  CLAUDE.md -> memory section already present, left alone"
fi
echo "done. Restart any running Claude Code session to pick this up."
