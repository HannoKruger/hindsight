#!/usr/bin/env bash
# Point a machine's Claude Code at Hindsight, and switch off the memory it ships with.
#
# Claude Code has its own memory store. Left enabled it competes with Hindsight: the
# model uses whichever the prompt mentions, and facts split across both. So this
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

# Fail before writing anything if the API is unreachable or the key is wrong.
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
        print("  settings.json is not valid JSON, refusing to overwrite", file=sys.stderr); raise SystemExit(1)
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
  \`<hindsight_memories>\` before every prompt. They are already in context. Read them, and do
  not go looking for a memory tool to fetch them, and never claim you have no memory.
- **Retention is automatic.** A \`Stop\` hook saves the transcript for fact extraction.
- **To save explicitly**, use whichever Hindsight tool the session has. Availability varies.
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

# Working and writing preferences. Kept separate from the memory section above so a
# machine can have one without the other: memory needs the API reachable, these do not.
if ! grep -q "^# How I want you to work" "$MD" 2>/dev/null; then
  cat >> "$MD" <<'PREFEOF'

# How I want you to work

- Answer or decision first, then only the reasoning that changes it.
- Pack the most information into the fewest words. Every sentence should carry a fact, a
  decision, or a consequence. Cut everything else.
- Work on your own. Do the research, make the call, verify it, then report what you did and
  what you found. Do not ask permission for steps you can check yourself. I make the direction
  and trade-off decisions, not the implementation ones.
- Never say something works unless you checked the real thing. Unverified work has unknown
  correctness. Say what you checked and what you did not.
- Be blunt. If my idea is bad, if I am working from a wrong assumption, or if the code is a
  mess, say so plainly and say why. Do not soften it and do not agree to be agreeable.
- Prefer open-source, self-hostable tools and the simplest thing that works. Prefer deleting
  over adding, and the smallest change that solves the problem. No over-engineering.
- Reading is always allowed. Read any file, config, log, database or key store you need to
  answer properly, without asking.
- Writing is the limit. Do not create, change, delete, rotate or deploy anything outside the
  task I gave you. Never modify credentials or shared config at all. Propose those instead.
- If you find yourself correcting the same thing repeatedly, say so and suggest a structural
  fix instead of just noting it again. A config change, a lint rule, or a CI check.

# Plans and design

- When I ask for a plan or a design, start at the top: how the system works, what the pieces
  are, how they fit together, and which decisions and trade-offs matter.
- Stop at that level. No file-by-file steps, no function signatures, no implementation detail
  unless I ask for it. I want to judge the shape of the system, not review the code twice.
- Name the trade-offs you chose against, and why. That is the part I need to decide on.

# Code

- Write code that explains itself. Almost never comment. If a line needs a comment to say what
  it does, rename or restructure it until it does not.
- The rare exception is a comment that says why, where the reason cannot live in the code: a
  workaround for an upstream bug, a non-obvious constraint, a decision that looks wrong until
  you know the history. Never a comment that restates the code.
- Full words in names. No abbreviations, no single letters except loop indices.
- Clean code principles: small functions, one responsibility each, names that state intent, no
  dead code, no duplication.
- If something should be refactored, refactor it. Do not ask first and do not leave it for
  later.

# How I want you to write

- Plain, everyday language. One thought per sentence. Instructions under 20 words, other
  sentences under 25. Active voice: name who does what.
- Use the simplest word: use not utilize, use not leverage, help not facilitate, buy not
  purchase. Use "is" instead of "serves as", "stands as", "boasts" or "features".
- Never use these words: delve, crucial, pivotal, showcase, tapestry, testament, underscore,
  landscape, intricate, interplay, enduring, enhance, additionally, vibrant, seamless, robust.
- No em dashes. Use a full stop, a comma, or brackets instead. Same for colons stuck in the
  middle of a sentence to join two thoughts. Split the sentence.
- No jargon or acronyms unless I used them first. If a technical term is unavoidable, explain
  it in a few words the first time. Use real names: the actual file, command, or number.
- Start with the content. No throat-clearing ("Here's the thing", "Let me be clear", "The
  truth is", "It turns out") and no filler ("At its core", "It's worth noting", "At the end of
  the day", "Make no mistake", "This matters because").
- No narrating what you are about to do ("Let me walk you through", "In this section we'll")
  and no sign-offs ("I hope this helps", "Let me know if").
- Drop the AI sentence shapes: no "not just X, but Y", no forced groups of three, no fake
  ranges ("from X to Y"), no rhetorical questions you then answer.
- Never write "simply", "just", "easy" or "quickly" about work I have to do. It is not simple
  if I am asking.
- Every "it", "this" and "they" must point at one obvious thing. Repeat the noun if unclear.
- Vary sentence length so it reads like a person. Sentence case headings.
- Plain does not mean vague. Keep the exact names, numbers, detail and any warning. Do not
  water it down or hide uncertainty behind simpler words.
PREFEOF
  echo "  CLAUDE.md -> preferences appended"
else
  echo "  CLAUDE.md -> preferences already present, left alone"
fi
echo "done. Restart any running Claude Code session to pick this up."
