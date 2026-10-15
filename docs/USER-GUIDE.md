# argus-desk user guide

argus-desk puts you between the model and the customer. Every few minutes it looks for open tickets
assigned to you and hands the next one to the model. The model reads it, writes a reply and files it at
the desk. You read it in the Desk pane and press **Send**, **Modify** or **Deny**. Nothing reaches the
customer without a Send; one Send allows exactly one post of exactly that text, and the post has to set the
ticket status you chose (Pending by default), or it is refused.

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

Leave the session open. The desk does the pulling; you do the deciding.

1. **The desk polls.** Every 5 minutes (option `pollMinutes`) it searches Zendesk through the model's own
   `invoke_proxy` tool for `type:ticket status<pending assignee:me` (option `searchQuery`): your new and
   open tickets. The pane's second line says when it last polled and whether the search failed.
2. **It pulls one ticket at a time.** SLA soonest first, then a ticket the customer has answered since we
   replied, then the oldest. It wakes the model with that ticket and the model reads every comment (the
   desk records them as the live thread), reads what the question needs, and then either files a reply
   or calls `skip_ticket` with a reason when nothing is owed right now. Nothing else is pulled while a
   ticket is being drafted or a draft is waiting for you. A pull that produces nothing in 30 minutes is
   offered again.
3. **The model files the draft.** It calls its `file_draft` tool with the exact reply. The desk runs its
   checks and puts the draft in the pane. The model is told to wait.
4. **You decide in the pane.** Type `/desk` if it is not already open. The pane shows:
   - the queue: `#ticket new | drafting | waiting | stale | sent | skipped`, with the SLA and subject.
   - one row per waiting draft: `#ticket md5 clear` or `#ticket md5 HOLD <reasons>`, its state and age.
     Click a row to select it.
   - `thread: 7 of 7 comments recorded`: the model read every comment the ticket has. `NOT captured`
     means it never fetched them, so the draft was written blind.
   - `claims: 0 of 2 receipted`: how many factual claims are backed by a recorded source. Advisory only.
   - `customer asked: ...` in yellow: questions from the customer that our side has not answered yet.
   - the draft text.
5. **Press one of the buttons** (or its key):
   - **Send, pending** (`s`): approves this exact text with the ticket going to Pending. The model is told
     to post it with that status in the same update, and the post goes through once.
   - **Send, solved** (`v`): the same, with the ticket going to Solved.
   - **Modify** (`m`): type one line saying what to change and press Return. The model revises and files a
     new draft, which comes back to the pane. The edit is recorded.
   - **Deny** (`d`): type why and press the deny button. The model is told not to post and to say what the
     ticket needs instead.

A Send lasts 6 hours. A draft nobody decides is dropped after 24 hours.

6. **The transition is checked twice.** Right after the post the desk reads the ticket back and the pane
   says `#N posted, status pending`; if the status did not move, it says so and the model is told to set
   it. On the next poll the ticket has left the open set, which the queue records as the transition
   confirmed. If the customer answers, the ticket comes back as `reopened` and is pulled first.

Hand controls: `/desk poll` polls now, `/desk pull 123456` pulls one ticket now, `/desk pause` and
`/desk resume` stop and start the timer. The default search assumes the Argus proxies `zendesk-full`
(reads) and `zendesk-write` (posts) and the endpoints `zendesk_search_tickets`, `zendesk_get_ticket`,
`zendesk_list_comments` and `zendesk_update_ticket`; if your Argus names them differently, set the
plugin's options (`/plugin` > argus-desk > configure) and the pane's poll line will tell you when a
search fails.

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
