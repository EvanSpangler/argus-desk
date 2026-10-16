# argus-desk user guide

argus-desk puts you between the model and the customer. Every few minutes it looks for open tickets
assigned to you and starts an investigation on the next one: three investigators from different angles, an
adversary that argues against them, a drafter that files a reply with a receipt for every factual sentence,
and a reviewer that may only object. You read the whole trace in the Desk pane, then press **Send**,
**Modify** or **Deny**. Nothing reaches the customer without a Send; one Send allows exactly one post of
exactly that text, and the post has to set the ticket status you chose (Pending by default), or it is refused.

## 1. Install (once per workstation)

You need Claude Code 2.1.296 or later and `python3` on the workstation.

1. Get the folder onto the workstation, at `~/argus-desk`. Either:
   - in a terminal: `git clone https://github.com/EvanSpangler/argus-desk ~/argus-desk`, or
   - if the workstation cannot reach GitHub (the Argus workstation has no outbound internet, so this is
     the route there): download the repository zip in your browser, upload it to the workstation (in Code
     OSS Explorer, right-click a folder under your home, not the home row itself, **Upload...**; the file
     lands in that folder), then in a terminal:
     `cd ~ && mv <folder>/argus-desk-main.zip . && { [ ! -e argus-desk ] || mv argus-desk argus-desk.prev; } && unzip -q -o argus-desk-main.zip && mv argus-desk-main argus-desk`.
     Never delete an existing folder to make room; rename it. A zip copy is not a git checkout, so the
     gate0 line `git` says `not-a-checkout`; that is expected.
2. Check it, in a terminal: `bash ~/argus-desk/gate0.sh`. Every line starts with `GATE0`. You want
   `validate ✔ Validation passed`, `plugin test 22 pass 0 fail` and `python tests OK`.
3. Turn it on: `bash ~/argus-desk/gate0.sh --install`. This adds one line to the `env` block of
   `~/.claude/settings.json` and changes nothing else.
4. Restart Claude Code (exit and start it again, or open a new terminal tab).

To try it for one session without installing, start Claude Code with
`claude --plugin-dir ~/argus-desk` instead of steps 3 and 4.

## 1a. Upgrade (every later version)

One script does the swap, the checks and the undo, from the copy that is already installed:

1. `bash ~/argus-desk/upgrade.sh --check` says what is installed, which zip is newest in `~/.cache`, and
   whether this host can fetch the zip itself (`fetch from GitHub: HTTP 200`).
2. With HTTP 200: `bash ~/argus-desk/upgrade.sh --fetch`. Otherwise download the repository zip in your
   browser, upload it as in section 1, and run `bash ~/argus-desk/upgrade.sh --from-zip ".cache/<name>.zip"`.
3. The script renames the old folder `argus-desk.prev-<stamp>`, unpacks the new one and runs gate0. If
   validate, the plugin tests or the python tests fail, it puts the old version back by itself and keeps the
   new folder as `argus-desk.failed-<stamp>`; nothing is ever deleted. The last line is `UPGRADE OK <version>`
   or `UPGRADE ROLLED BACK`.
4. Restart Claude Code (`/exit`, then your launcher) and run `/desk`.

Undo: `bash ~/argus-desk/upgrade.sh --rollback` restores the newest `argus-desk.prev-*`, then restart.
Versions before 0.6.3 have no `upgrade.sh`: do the section 1 swap by hand once, after that use the script.

## 2. Check it is on

Type `/desk`. You should see the Desk pane with "No drafts filed." If `/desk` is an unknown command,
the mod did not load: see Troubleshooting. Type `/desk` once per session anyway: the desk only starts
runs after a `/desk` command (a run pulled before that waits, and the Tickets pane says so). Then
`/desk poll`: it must name the open tickets or print the proxy's own answer, which is where a wrong
proxy or endpoint name shows.

## 3. Daily use

Leave the session open. The desk does the pulling and the investigating; you do the deciding.

1. **The desk polls.** Every 5 minutes (option `pollMinutes`) it searches Zendesk through the model's own
   `invoke_proxy` tool for `type:ticket status<pending assignee:me` (option `searchQuery`): your new and
   open tickets. The Tickets pane says when it last polled and whether the search failed.
2. **It runs one ticket at a time.** SLA soonest first, then a ticket the customer has answered since we
   replied, then the oldest. A run is a tree of subagents inside this session:
   - three **investigators** in parallel (docs first; evidence first; thread first), each on a cheap model.
     Each must record at least 2 receipts; one that finishes under that floor is sent back in once, told
     what it recorded, before anything else moves;
   - an **adversary** that assumes the leading explanation is wrong and records counters;
   - a **drafter** that writes the reply and files it with a citation for every factual sentence, under
     the standing rules (answer the need first, docs link over steps, no unconfirmed wording, no echo of
     the customer's words, no sign-off, the escalation phrase);
   - two independent **reviewers** per round, each with its own checklist: A reads the thread (every
     question answered, asked back or stated unknown with an action; constraints; silent reversals;
     re-asks; ignored material), B reads the evidence (each receipt says what the sentence says; platform
     fit; the first paragraph answers the need; escalation backed by a documented mismatch; commitments).
     They may only object. One objection from either sends the draft back to the drafter, up to
     `maxRounds` times (default 2); then it comes to you with the objections on the trace.
   Nothing else is pulled while a run is on or a draft is waiting for you. `/desk run 123456` starts a run
   by hand and holds the prompt until it ends; `/desk pull 123456` starts one in the background.
3. **Receipts.** An agent can only cite what it actually read: a sentence verbatim from an official
   documentation page the session fetched (`docs`), or the customer's own words from the captured thread
   (`material`). A quote that is not there is refused. The draft's claims are checked against them.
   Documentation is read through the session's docs search tool (option `docsTool`, default
   `enterprise_search`, on the data source `docsSource`, default `wiz_docs_public`): every result is on the
   record and kept under each official URL it carries, so a docs receipt quotes the result and cites one of
   those URLs. Such a receipt is marked `(search result)` on the trace and in the audit: the quote was
   checked against the search result that names the URL, not against the page itself. A receipt on a page
   the agent fetched by its URL through the proxy carries no mark. The agents run with every tool the
   session has; the gate holds on the proxy.
4. **You read the proof, then decide.** `/desk` opens the Desk pane with three views: **Review**,
   **Tickets** and **Wall**. Its first line names them, the one showing in brackets (`1: [Review]   2: Tickets
   3: Wall`); the keys `1` to `3` switch. (Before 0.8.1 each view was a pane of its own, and Claude Code
   refuses to move the keyboard from one pane to another, so `1` to `3` did nothing on a real terminal.)

   The pane takes the keyboard after ctrl+x then tab, or a click on it; until then its second line reads
   `keys off` and says so. Every key that does something is drawn on the pane as a button with its key
   (`z: zoom into the row`), on bars at the top of the view, so the key list is on screen when a view opens:
   nothing works that is not drawn. Tab walks the rows, the arrows scroll, Enter acts on the row under the
   ring, Esc hands the keys back to the prompt. A row cut at the pane's edge shows `+`; Enter on it shows the
   whole text (a customer message, an objection). Every view has `f: find` (type, Enter narrows the rows to
   what matches and hands the keys back to the view) and `c: clear`.
   - **Review** is the proof tree behind the selected draft, the record made navigable. Its sections:
     ISSUES (everything open: reviewer objections, sentences with no receipt or a problem, thread checks,
     a ticket updated after the thread was recorded), ASK (the customer's open questions, the ask as the
     thread-first investigator read it, the thread itself with the customer in yellow), REPLY (every
     sentence with a provenance mark: `[docs+material]`, `[docs]`, `[material]`, `[search]` for a docs
     quote checked against a search result rather than the page, `[none]`, `[echo]`, `[-]` for a sentence
     that asserts nothing; under each sentence its receipts with the verbatim quote and who minted it,
     the page or the message around the quote, the findings that carry it, and what argued against it),
     DEBATE (every hypothesis, counter and objection with its answers and its state: open, answered,
     overruled), RUN (each agent with its model, time and summary; its prompt, its answer and every call
     it made) and SEND (the exact text and its hash, the status it will set, the grant's life, and after
     a Send the decision, the post and the transition). Enter opens or closes a row. `z: zoom into the
     row` makes it the root (the line above the tree shows the path), `u: up` comes back, `x: expand all
     beneath`, `g: go to what the row is about` follows an issue to its sentence or a receipt to the agent
     that minted it, `i t r w` jump to ISSUES, the thread, REPLY and RUN, `o: the ticket` opens it in
     Tickets, `n: next draft`. Marks are provenance facts, not grades.
   - **Tickets**: the open set, one row per ticket: state (`new`, `drafting`, `waiting`, `stale`,
     `sent`, `skipped`), SLA, age, subject. Enter opens the row to its thread record, its run and its
     drafts; Enter on a draft puts it in Review. `p: pull the ticket under the ring`, `r: re-read its
     thread` (the model lists the comments again so the record is fresh before a Send), `o: its draft in
     Review`.
   - **Wall**: every agent on every ticket, live, one row each: role, label, minutes running, its last
     tool call or sentence. Enter tails that agent; `b: back`. This is where you watch the work.

   The same tree as text: `/desk show` (the selected draft), `/desk show <ticket>`, `/desk show <md5>`,
   with a node id after it for one branch (`/desk show 123456 reply`), and `desk.py tree --text` from a
   shell.
5. **Press one of the buttons** (or its key):
   - **Send, pending** (`s`): approves this exact text with the ticket going to Pending. The model is handed
     the text and told to post it with that status in the same update, and the post goes through once.
   - **Send, solved** (`v`): the same, with the ticket going to Solved.
   - **Modify** (`m`): type one line and press Return. The drafter revises with your note and files the new
     text; the reviewer looks again; it comes back to Review. Your note is kept as an objection on the trace.
   - **Deny** (`d`): type why. Nothing posts; the reason is recorded on the trace.

   Modify and Deny put the cursor in their field, with a `cancel` button beside it (Tab, Enter) that leaves
   the draft undecided. After any decision the next draft opens with the ring on its first row, not on Send.

A Send lasts 6 hours. A draft nobody decides is dropped after 24 hours. A ticket has one draft at the desk: a
redraft or a new run's draft withdraws the ticket's older undecided one (the ledger says which replaced it), and
a Send on the older one is refused.

6. **The transition is checked twice.** Right after the post the desk reads the ticket back and the pane
   says `#N posted, status pending`; if the status did not move, it says so and the model is told to set
   it. The readback also records the ticket's `updated_at` after our post. On the next poll the ticket has
   left the open set, which the queue records as the transition confirmed. Zendesk's search index lags by
   a few minutes, so a ticket we just posted to may still show in the open set once or twice: it stays
   `sent` until its `updated_at` moves past the readback (or, with no readback, more than two minutes past
   the Send). If the customer answers, the ticket comes back as `reopened` and is pulled first.

## 3a. Accountability: the record behind a reply

Every reply can be walked backwards to its evidence and its authors. `/desk audit N` (or `/desk audit <md5>`)
prints the chain for the ticket's latest draft: the exact bytes and their hash, the readback after posting,
who pressed and when, any HOLD the Send overruled, the checks at filing, then each factual sentence with
its receipt, the receipt's source and the captured bytes it was checked against (file and hash), the agent
that minted it, that agent's model and prompt hash; then every agent in the run with its prompt, its full
answer and every proxy call it made (arguments and a hash of the result); then the captures; then the
integrity check. The records live under `~/.argus-desk`: `runs/N/events.jsonl` (the tail), `findings.jsonl`,
`calls.jsonl` (every call), `prompts/<agent>.txt`, `answers/<agent>.txt`, `threads/N.json` and `pages/`
(what was read), `receipts.jsonl`, `requests.jsonl`, `decisions.jsonl`, `transitions.jsonl`. Every record
the desk writes is chained (`prev`, `h`: a sha256 over the previous hash and the row); `/desk verify` walks
every chained file and names the first broken link, so a record edited, dropped or inserted by hand shows.
The chain has no secret: a rewrite of a whole file that recomputes every hash passes `verify`, so the chain
is a check against accidents and casual edits, not proof against someone with the desk's own code and write
access to `~/.argus-desk`. If that matters for your audit, copy the `h` of the last row of each file
somewhere the workstation cannot write (the `/desk verify` output in the transcript does), and compare. A
refused receipt is on the tail too, with the quote the agent claimed and why it was refused. A ticket run
again (after a Deny, or by hand) keeps its earlier run under `history` in `runs/N/run.json` and starts its
own review rounds.

Hand controls: `/desk run N`, `/desk trace N` (the trace as text in the transcript), `/desk audit N`,
`/desk verify`, `/desk poll`,
`/desk pull N`, `/desk pause`, `/desk resume`, `/desk stats`. The default search assumes the Argus proxies
`zendesk-full` (reads) and `zendesk-write` (posts) and the endpoints `zendesk_search_tickets`,
`zendesk_get_ticket`, `zendesk_list_comments` and `zendesk_update_ticket`; if your Argus names them
differently, set the plugin's options (`/plugin` > argus-desk > configure). Models per role are options
too (`investigatorModel`, `adversaryModel`, `drafterModel`, `reviewerModel`); the docs hosts a receipt may
cite are `docsHosts`.

## 4. What the gate does

Any call the model makes through `invoke_proxy` that is not plainly a read (an endpoint named `..._get`,
`_list`, `_search`, `_read`, `_show`, `_fetch`, `_count` or `_describe`) is treated as outward, on every
proxy, Zendesk or not. An outward call goes through only if its comment body (a field named `body`,
`html_body`, `plain_body`, `text`, `message`, `content`, `comment`, `note` or `description`) is exactly the
text you sent from the pane, and that Send is then spent. Text anywhere else in the call (a tag, a subject,
a custom field) does not count. Otherwise the model gets:

> argus-desk: this call would post to the customer and carries no text the operator has granted.

The post is also refused, with the approval left open, when:

- it carries a second body text beside the granted one (for example an `html_body` that differs);
- the comment is marked `public: false` (an internal note is not the reply you approved);
- the ticket `status` it sets is not the one your Send chose. The status is read from the ticket object
  or the top of the arguments, not from a nested field. The model is told to repeat the call with the
  status.

The match is exact. If the text is changed after your Send, even only reformatted, the post is refused
and the model has to file it again. One known case: copying a reply out of the Zendesk composer drops
numbered-list markers ("1. ", "2. "), so the copied text no longer matches.

**What the gate protects against, and what it does not.** The gate runs as a hook on the model's tool
calls, so it holds whether or not Claude Code's own permission prompts are on: a reply the model never
filed, or that you did not grant, or that was edited after the grant, cannot go out through the proxy by
mistake. It is not a control against a model that sets out to route around it. The grants and the record
are files under `~/.argus-desk`, written by `checks/desk.py`, and the model's shell could run that script
or edit those files. The desk therefore refuses a `Bash`, `Write` or `Edit` call that names `desk.py`,
`desk_mcp.py` or the state directory (option `guardRecord`, on by default; the refusal is on the tail of
the run as a `guard` line). That closes the plain routes; it does not make the workstation a sandbox. Run
desk commands yourself in a terminal, never by asking the model to.

## 5. What a HOLD means

A HOLD is advice for you, not a block. You can still press Send.

The draft's own text:

| Code | Meaning |
|---|---|
| `dash` | The draft contains an en or em dash. |
| `timeline-commitment` | It promises a time ("shortly", "by tomorrow", "within 2 days"). |
| `requester-name-unrecorded` | The draft greets someone by name and no requester name is on record for the ticket. The desk records it when a ticket read shows the requester field; `desk.py requester --ticket N --name "<exact name>"` records it by hand; a requester registry exported from your own tooling in the same shape (`{"<ticket>": {"name": "Surname, Given"}}`) can be copied to `<stateDir>/requesters.json`. |
| `requester-name-mismatch` | The greeted name is not a part of the recorded requester name ("Hi Alvar" to Alba). |
| `ticket-denied-recently` | You denied a draft on this ticket in the last 72 hours. The drafting has been wrong here, so the decision is yours: a delegated press under the usual bar would be held. A Modify is not a deny. |
| `sends-held` | The file `<stateDir>/sends-held` exists: every draft holds until you remove it. |
| `escalation-without-mismatch` | The draft escalates, or says it did, and no fresh mismatch is on record: the drafter records one with `desk_mismatch` (a docs receipt for what the product should do, a material or test receipt for what it did, one line on how they differ). |
| `voice:<rule>` | A block rule from your voice rules matched ("absolutely right", a template placeholder, AI self-reference, a vendor or model name, a typed sign-off). Warn rules appear as warnings. |

The thread it answers:

| Code | Meaning |
|---|---|
| `thread-not-live`, `capture-missing` | No live read of this ticket's comments. |
| `capture-stale`, `thread-stale` | The comments were read more than 15 minutes before filing. Ask the model to re-read them. |
| `unrecorded-comment` | The ticket has more comments than the model read. |
| `too-soon` | We replied publicly less than 4 hours ago. |
| `too-many` | Two or more of our public replies in the last 12 hours. |
| `repeat-ask` | It asks the customer again while our last question is still unanswered. |
| `customer-cannot-act` | Their latest message asks for steps, asks what we want, or says they are confused, and the draft has neither numbered steps nor a plain imperative ask. |
| `holding-reply` | They asked a question and the draft says we are still checking without naming an action (a reproduction, an escalation, a test). |
| `review:<code>` | A reviewer's objection is open against this draft and quotes it. The code is the fleet's reply-review code the reviewer named (`misattributed`, `question-not-answered`, `contradicts-customer`, `implied-commitment` ...), or `objection`. It clears when the drafter answers it; a Send names it as overruled. |
| `announced-reversal` | It announces a reversal of something we already told the customer ("my last message", "correction:"). |

The customer's open questions since our last reply are listed under ASK in the Review pane; they
are for you to check, not a HOLD code (the thread reviewer raises an objection for each one the draft
leaves unanswered).

The claims it makes. Every checkable sentence (an instruction, a cause, a status, a product behaviour, a
statement about the customer's setup, or a sentence naming a setting, file, version or quantity) needs a
receipt that says what it says: official documentation or a recorded test for product behaviour, the
customer's own material for their setup, both legs for a cause.

| Code | Meaning |
|---|---|
| `uncovered` | A checkable sentence has no receipt bound to it (the drafter cites each sentence with `file_draft`, or the receipt's quote is the sentence). |
| `receipt-stale` | Its only receipts are older than 24 hours. |
| `receipt-unrelated` | The receipt shares no content words with the sentence. |
| `quote-does-not-support` | The receipt covers less than half of the sentence's content words. |
| `partial-binding` | Three or more of the sentence's content words, a fifth of them or more, are in neither the quote nor the cited fragment. |
| `element-not-in-evidence` | A setting, path, version or quantity the sentence names is not in the quote. |
| `instruction-needs-docs-or-test`, `behaviour-needs-docs-or-test` | A step or a product behaviour is backed only by the customer's material. |
| `cause-needs-product-leg-and-material` | A cause is asserted with only one of its two legs (docs mechanism, customer's own data). |
| `conditional-paraphrase` | The sentence says "only if", "unless", "requires" and the docs quote does not. |
| `polarity-not-in-quote` | The sentence is negative ("does not", "cannot") and the quote is not. |
| `hedged-claim` | A receipted sentence still hedges ("may", "likely", "appears to"): verified means say it plainly. |
| `unconfirmed-wording` | "I suspect", "unconfirmed" or similar: a claim is verified or cut. |
| `snippet-untested` | A code block has no test receipt recording that it was run. |
| `echo-only` | Every sentence that would be credited only restates the customer. |

Warnings (shown, never a HOLD): `no-thread-captured`, `echo-share` (a quarter or more of the draft's words sit in
runs of six copied from the customer), and `voice:<rule>` for warn-level voice rules.

These codes are the fleet's usual bar as well as the desk's: since 0.8.2 the requester name, the 72-hour deny
hold, the kill switch, the escalation basis, the binding thresholds (3 words, 20%), the reversal phrases and the
six-word echo measure are the fleet's own, so a draft that clears here clears there.

Your voice rules live in `<stateDir>/voice-rules.json` (`{"rules": [{"id", "pattern", "severity":
"block|warn", "channels": ["all"]}]}`), exported from your own lint; without the file a built-in floor
applies ("you're absolutely right", template placeholders, AI self-reference, vendor names, a typed
sign-off). Dashes are the separate `dash` check, so one dash is one hit.

## 6. Numbers

`/desk stats` prints: drafts filed, how many were sent as filed, sent after a Modify, or denied, the wait
from filing to your decision, posts through the gate and how many status transitions the next poll
confirmed, the queue counts, and how often a HOLD was overruled. This is what the pilot is judged on.
A run costs one context per agent (eight to seventeen per ticket: three investigators plus any sent back
in, the adversary, the drafter, two reviewers per round); the investigators and reviewers default to
Sonnet, the adversary and drafter to Opus.

## 7. Turn it off

`bash ~/argus-desk/gate0.sh --remove`, then restart Claude Code. The desk's records stay in
`~/.argus-desk`; delete that folder to clear them.

## 8. Troubleshooting

- **`/desk` is unknown.** The mod is not loaded. Run `bash ~/argus-desk/gate0.sh` and check the line
  `settings env:` shows the folder path, then restart Claude Code.
- **The model says it has no `file_draft` tool.** Same cause. Also check Claude Code was not started with
  `--tools ""`, which hides it.
- **`Not filed: already filed and undecided`.** That exact text is already waiting in the pane. Decide it.
- **`Not filed: desk.py exit ...`.** `python3` is missing or failed. Run `python3 --version`.
- **The poll line says `Poll failed: <proxy> <endpoint> answered: ...`** or `Polled 0 ... The proxy
  answered: ...`. That is the proxy's own reply to the search the desk sent, with the proxy and endpoint
  names it used. An HTTP 404 or "unknown endpoint" means the names differ from the defaults: set the
  plugin's options (`/plugin` > argus-desk > configure) to the names your Argus lists for `invoke_proxy`.
  A `{"results":[]}` means the search query matched nothing. The default `assignee:me` is the proxy's own
  account, not you, when the proxy runs under a service account (it did on the Argus workstation): set the
  query to your Zendesk user id with `/desk query type:ticket status<pending assignee:<id>` (kept for every
  session on the workstation; `/desk query` shows it, `/desk query default` restores the option).
- **The pane ignores the keys.** Its second line reads `keys off`: press ctrl+x then tab, or click the pane. A
  field with the cursor in it (a Modify note, a Deny reason) takes every key until Enter; Tab reaches its
  `cancel` button, which leaves the draft undecided. Esc hands the keyboard back to the prompt. Without the keys, the pane
  commands (`/desk review`, `/desk tickets`, `/desk wall`, `/desk show`) still work.
- **A run finished in seconds with no receipts.** Look at RUN in the Review pane, `/desk show N run`, or `/desk trace N`: an agent that
  says it had no proxy tool or that a desk tool was not answered means the desk was started outside a
  `/desk` command; type `/desk` and `/desk run N` again.
- **The agents still report "no tool.call hook answered" for desk_receipt or file_draft.** On that host a
  hook does not answer inside a spawned agent. Since 0.6.2 the agents get the desk tools from the plugin's
  own MCP server by default, so this needs no setting: check `bash ~/argus-desk/gate0.sh --diag` shows the
  plugin's pluginConfigs without `"agentTools": "hooks"`; if an older install set it, `bash ~/argus-desk/gate0.sh
  --mcp` clears it back (`--hooks` is the opt-out). Restart `argus` and run the ticket again. `bash ~/argus-desk/gate0.sh --diag` prints how the plugin is loaded on the host (launcher,
  env, settings, plugin list) and the agents recorded on each run, for the report. The tools then come from the plugin's own MCP server (`checks/desk_mcp.py`, one per agent
  type, the same record on disk); the main session keeps the hook-served ones. `/desk trace N` shows the
  agent as the author of each finding.
- **Nothing gets pulled although tickets are open.** The pane shows `poll paused` (`/desk resume`), or a
  ticket is `drafting` or `waiting`: decide it, or `/desk pull N` to force another.
- **The model says `argus-desk: Bash names .argus-desk, which only the desk may write`.** The record guard
  (section 4). The model was asked, or decided, to run a desk command or touch the state directory; do that
  yourself in a terminal. To work on the desk's own code from inside a session, set the option
  `guardRecord` to false for that session.
- **A ticket we just answered shows as `reopened` and is pulled again.** Its `updated_at` moved past the
  readback after our post, which means someone touched it after us. If it was a bot or a colleague, not the
  customer, `/desk` shows the run and you can Deny the new draft; the `sent` line stays on the record.
- **Everything shows `thread: NOT captured`.** The model drafted without reading the comments. Ask it to
  read the ticket's comments, then file again.

**A receipt is refused on words the agent did read.** Since 0.6.4 the refusal says what the desk holds. `no-thread-captured`: no comments read of that ticket reached the desk in this run, so the agent must read every comment through the proxy first (the desk records that read as the thread). `quote-not-in-material` now names how many customer messages and characters the thread holds; `quote-not-from-customer` means the words are in one of our own replies. `page-not-captured` lists the documentation URLs captured so far; a page is found under its URL however it is written (with or without https://, trailing slash, or anchor). The refusals are on the ticket record as `receipt-refused` events, visible in `/desk trace N`.
