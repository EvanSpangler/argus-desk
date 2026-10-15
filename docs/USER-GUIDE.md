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
   - if the workstation cannot reach GitHub: download the repository zip in your browser, upload it to
     the workstation (in Code OSS: right-click a folder in Explorer, **Upload...**), and unzip it so the
     folder is `~/argus-desk`.
2. Check it, in a terminal: `bash ~/argus-desk/gate0.sh`. Every line starts with `GATE0`. You want
   `validate ✔ Validation passed`, `plugin test 7 pass 0 fail` and `python tests OK`.
3. Turn it on: `bash ~/argus-desk/gate0.sh --install`. This adds one line to the `env` block of
   `~/.claude/settings.json` and changes nothing else.
4. Restart Claude Code (exit and start it again, or open a new terminal tab).

To try it for one session without installing, start Claude Code with
`claude --plugin-dir ~/argus-desk` instead of steps 3 and 4.

## 2. Check it is on

Type `/desk`. You should see the Desk pane with "No drafts filed." If `/desk` is an unknown command,
the mod did not load: see Troubleshooting.

## 3. Daily use

Leave the session open. The desk does the pulling and the investigating; you do the deciding.

1. **The desk polls.** Every 5 minutes (option `pollMinutes`) it searches Zendesk through the model's own
   `invoke_proxy` tool for `type:ticket status<pending assignee:me` (option `searchQuery`): your new and
   open tickets. The Queue tab says when it last polled and whether the search failed.
2. **It runs one ticket at a time.** SLA soonest first, then a ticket the customer has answered since we
   replied, then the oldest. A run is a tree of subagents inside this session:
   - three **investigators** in parallel (docs first; evidence first; thread first), each on a cheap model;
   - an **adversary** that assumes the leading explanation is wrong and records counters;
   - a **drafter** that writes the reply and files it with a citation for every factual sentence;
   - a **reviewer** that may only object (unanswered question, contradiction with the thread, a step that
     does not fit their platform, a sentence without a receipt). One objection sends it back to the
     drafter once (option `maxRounds`); then it comes to you.
   Nothing else is pulled while a run is on or a draft is waiting for you. `/desk run 123456` starts a run
   by hand and holds the prompt until it ends; `/desk pull 123456` starts one in the background.
3. **Receipts.** An agent can only cite what it actually read: a sentence verbatim from an official
   documentation page the session fetched (`docs`), or the customer's own words from the captured thread
   (`material`). A quote that is not there is refused. The draft's claims are checked against them.
4. **You read the trace, then decide.** `/desk` opens the pane. Four tabs, keys `1` to `4`, `j`/`k` to
   scroll, `n` for the next draft:
   - **Wall**: every agent on every ticket, live: role, model, minutes running, its last tool call or
     sentence. This is where you see what it is working through.
   - **Queue**: the open set and where each ticket stands (`new`, `drafting`, `waiting`, `stale`, `sent`,
     `skipped`).
   - **Ticket**: the trace of the selected ticket: phase, each agent and its summary, then HYPOTHESIS,
     EVIDENCE, COUNTER, OBJECTION, ANSWER, SUMMARY findings with their receipt ids, the RECEIPTS with their
     sources and quotes, and CLAIMS IN THE DRAFT: each factual sentence marked `ok <receipt>`, `uncovered`,
     `echo` (restates the customer) or the specific problem. Read this before you Send.
   - **Decide**: the draft, its checks, the customer's open questions, the reviewer's objections, and the
     buttons.
5. **Press one of the buttons** (or its key):
   - **Send, pending** (`s`): approves this exact text with the ticket going to Pending. The model is told
     to post it with that status in the same update, and the post goes through once.
   - **Send, solved** (`v`): the same, with the ticket going to Solved.
   - **Modify** (`m`): type one line and press Return. The drafter revises with your note and files the new
     text; the reviewer looks again; it comes back to Decide. Your note is kept as an objection on the trace.
   - **Deny** (`d`): type why. Nothing posts; the reason is recorded on the trace.

A Send lasts 6 hours. A draft nobody decides is dropped after 24 hours.

6. **The transition is checked twice.** Right after the post the desk reads the ticket back and the pane
   says `#N posted, status pending`; if the status did not move, it says so and the model is told to set
   it. On the next poll the ticket has left the open set, which the queue records as the transition
   confirmed. If the customer answers, the ticket comes back as `reopened` and is pulled first.

Hand controls: `/desk run N`, `/desk trace N` (the trace as text in the transcript), `/desk poll`,
`/desk pull N`, `/desk pause`, `/desk resume`, `/desk stats`. The default search assumes the Argus proxies
`zendesk-full` (reads) and `zendesk-write` (posts) and the endpoints `zendesk_search_tickets`,
`zendesk_get_ticket`, `zendesk_list_comments` and `zendesk_update_ticket`; if your Argus names them
differently, set the plugin's options (`/plugin` > argus-desk > configure). Models per role are options
too (`investigatorModel`, `adversaryModel`, `drafterModel`, `reviewerModel`); the docs hosts a receipt may
cite are `docsHosts`.

## 4. What the gate does

Any call the model makes through `invoke_proxy` that is not plainly a read (an endpoint named `..._get`,
`_list`, `_search`, `_read`, `_show`, `_fetch`, `_count` or `_describe`) is treated as outward, on every
proxy, Zendesk or not. An outward call goes through only if it carries text you sent from the pane, and
that Send is then spent. Otherwise the model gets:

> argus-desk: this call would post to the customer and carries no text the operator has granted.

This holds even when Claude Code runs with permission prompts switched off.

The post must also set the status your Send chose. A post that carries the approved text without the
status is refused with the approval left open, and the model is told to repeat the call with the status.

The match is exact. If the text is changed after your Send, even only reformatted, the post is refused
and the model has to file it again. One known case: copying a reply out of the Zendesk composer drops
numbered-list markers ("1. ", "2. "), so the copied text no longer matches.

## 5. What a HOLD means

A HOLD is advice for you, not a block. You can still press Send.

| Code | Meaning |
|---|---|
| `dash` | The draft contains an en or em dash. |
| `timeline-commitment` | It promises a time ("shortly", "by tomorrow", "within 2 days"). |
| `capture-stale`, `thread-stale` | The comments were read more than 15 minutes before filing. Ask the model to re-read them. |
| `unrecorded-comment` | The ticket has more comments than the model read. |
| `thread-not-live`, `capture-missing` | No live read of this ticket's comments. |
| `too-soon` | We replied publicly less than 4 hours ago. |
| `too-many` | Two or more of our public replies in the last 12 hours. |
| `repeat-ask` | It asks the customer again while our last question is still unanswered. |
| `announced-reversal` | It contradicts something we already told the customer. |
| `question-not-answered` | The customer asked something the draft does not address. |

Warnings (shown, never a HOLD): `no-thread-captured`, and `claims:...` when claims have no receipts.

## 6. Numbers

`/desk stats` prints: drafts filed, how many were sent as filed, sent after a Modify, or denied, the wait
from filing to your decision, posts through the gate and how many status transitions the next poll
confirmed, the queue counts, and how often a HOLD was overruled. This is what the pilot is judged on.
A run costs one context per agent (six to eight per ticket); the investigators and reviewer default to
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
- **The poll line says `failed: no invoke_proxy tool`** or names an endpoint error. The session has no
  `invoke_proxy`, or the endpoint names differ from the defaults: set the plugin's options.
- **Nothing gets pulled although tickets are open.** The pane shows `poll paused` (`/desk resume`), or a
  ticket is `drafting` or `waiting`: decide it, or `/desk pull N` to force another.
- **Everything shows `thread: NOT captured`.** The model drafted without reading the comments. Ask it to
  read the ticket's comments, then file again.
