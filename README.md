# argus-desk

A review desk inside Claude Code 2.1.296 or later for an agent that drafts customer replies. The model
files each reply with the `file_draft` tool; the desk checks it against the live thread it captured from the
agent's own comment fetches; the operator presses Send, Modify or Deny in the `/desk` pane; the posting
tool is refused unless the comment body is exactly the text that was granted, the comment is public and the
ticket status is the one the Send chose, and one grant allows one post. `/desk stats` prints the decision
table: sent as filed, modified, denied, wait, check holds.

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

The same three commands run in CI on every push and pull request (`.github/workflows/check.yml`).

## What the gate is, and is not

The gate stops the model from posting by mistake: text it never filed, text the operator did not grant,
text edited after the grant, a second post on one grant, an internal note instead of a public comment, a
post without the status the operator chose. It reads the comment body fields of the call and nothing else.

It is not a sandbox. The grants and the record live in files under `stateDir`, written by `checks/desk.py`,
and a model with a shell could mint a grant or rewrite the record by running that script or editing those
files. The desk refuses a `Bash`, `Write` or `Edit` call that names `desk.py` or the state directory
(option `guardRecord`, on by default), which closes the plain routes; a determined agent with unrestricted
shell access still has others. The record's hash chain (`/desk verify`) shows an edit that did not recompute
the hashes after it; it has no secret, so a rewrite of a whole file with the desk's own code passes. Treat
the desk as the operator's review step and the audit as the operator's record, not as a control against an
adversarial model.

## Layout

- `hooks/register.tsx`: the `file_draft` tool, the send gate on the outward tool, the record guard, comment
  capture, the pane, `/desk`.
- `checks/desk.py`: state and checks, stdlib Python; `confirm_ledger.py`, `claim_receipts.py`,
  `thread_review.py` are the decision rules it binds; `desk_mcp.py` serves the desk tools to the agents over
  MCP when `agentTools=mcp`.
