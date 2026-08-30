#!/bin/sh
# Bring the host's Claude Code login into the container.
#
#   $HOME/.claude              bind-mounted host ~/.claude (holds .credentials.json —
#                              shared, so token refreshes stay in sync with the host)
#   /host-claude/.claude.json  read-only mount of host ~/.claude.json; copied rather
#                              than mounted in place because the CLI rewrites it via
#                              rename, which breaks single-file bind mounts.
set -e

if [ -f /host-claude/.claude.json ] && [ ! -f "$HOME/.claude.json" ]; then
    cp /host-claude/.claude.json "$HOME/.claude.json"
fi

if [ ! -f "$HOME/.claude/.credentials.json" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "WARNING: no $HOME/.claude/.credentials.json and no ANTHROPIC_API_KEY -" \
         "Claude CLI calls will fail with 'Not logged in'." >&2
fi

exec python3 server.py
