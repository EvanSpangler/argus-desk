# argus-desk

A review desk inside Claude Code 2.1.296 for an agent that drafts customer replies. The model files
each reply with the `file_draft` tool; the desk checks it against the live thread it captured from the
agent's own comment fetches; the operator presses Send, Modify or Deny in the `/desk` pane; the posting
tool is refused unless the exact text was granted, and one grant allows one post. `/desk stats` prints
the decision table: sent as filed, modified, denied, wait, check holds.

## Install

Step by step, with what the pane shows and what each button does: [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

From a terminal session of Claude Code:

```
/plugin install argus-desk --marketplace EvanSpangler/argus-desk
```

Or run from a folder: clone this repository and start Claude Code with `--plugin-dir <folder>`, or add
the folder to `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `~/.claude/settings.json`. Needs `python3`.
State is kept in `~/.argus-desk` (change it with the plugin's `stateDir` option).

## Check it

```
claude plugin validate .
claude plugin test .
cd checks && python3 -I -m unittest discover -s tests
```

## Layout

- `hooks/register.tsx`: the `file_draft` tool, the send gate on the outward tool, comment capture, the pane, `/desk`.
- `checks/desk.py`: state and checks, stdlib Python; `confirm_ledger.py`, `claim_receipts.py`, `thread_review.py` are the decision rules it binds.
