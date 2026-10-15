# The desk interface: design

Status: BUILT in 0.8.0, 2026-10-15 (three panes; Send at the operator's discretion, the overrule logged as today, no reason
required; provenance marks, no grades). Standing rule from the operator: every navigation key and action is visible
and labelled on screen, drawn as the engine draws a hotkey button (`z: zoom`); arrows, and Esc, Enter and Space
where their meaning is plain, are the only unlabelled keys. The 0.7.0 pane (four panes,
rows, filters) is the scaffolding this replaces; its Wall and Tickets parts survive, its Ticket and Decide
views do not.

## 1. What the desk is for

The desk exists so that the operator can press Send on a customer reply with the proof in hand, and so that
every press is accountable afterwards. Everything else (the poll, the queue, the investigators, the
adversary, the reviewers, the wall) is how the proof gets produced. So the interface is not a dashboard of
agents with a draft at the end. It is the answer and its proof, and the machinery sits beneath the proof,
reachable by zooming in.

The record already holds the chain (`desk audit`): what went out, who pressed, what the checks said, each
sentence and its receipt, each receipt's source and captured bytes, the agent that minted it, that agent's
model, prompt, answer and every call it made, hash-chained. The interface is that chain, navigable.

## 2. What the operator must be able to answer before Send

Each question, where the answer already lives in the record, and how the interface shows it.

| # | Question | In the record | Shown as |
|---|----------|---------------|----------|
| 1 | What did the customer actually ask, and does the first paragraph answer it? | thread store (messages, who, at), thread check `open_questions`, reviewer A's first-paragraph objection | ASK node at the top of the tree: open questions, then the thread, customer in yellow |
| 2 | Is every factual sentence backed? | claims check per sentence: receipts, problems, echo, classes | REPLY node: one row per sentence with a provenance mark |
| 3 | Is the receipt real, and does it say what the sentence says? | receipts.jsonl (kind, source, quote, bound, page_origin fetched or search), pages store, reviewer B's objection | sentence > receipt: kind, source, the verbatim quote, who minted it, when, fetched page or search result; expands to the page excerpt or the thread message in context, and reviewer B's judgement |
| 4 | Where is the mechanism and where is the evidence (the two legs)? | claim classes (cause, behaviour, instruction, status, environment), receipt kinds (docs, material) | a causal sentence shows two legs, docs and material, each present or MISSING |
| 5 | What was argued against it, and was that answered? | findings: counter, objection, answer, with `about` links; rounds | DEBATE node: each objection threaded with its answers; state open, answered, overruled |
| 6 | What is the desk holding on, exactly where? | checks at filing: verdict, hits, warnings, lint; thread check mechanical and capture | ISSUES node, first under the root: each issue names its sentence or node and zooms to it |
| 7 | Has the ticket moved since the proof was made? | thread fetched_at and count receipt; queue row updated_at and sent_updated | ASK node header: recorded n of m comments, age; TICKET CHANGED SINCE THE RECORD in red when updated_at is later |
| 8 | Who did what, with what model, in how long? | run.json agents with prompt and answer sha and files, calls.jsonl, events.jsonl | RUN node: agents; each expands to summary, prompt, answer, calls, refused receipts |
| 9 | What exactly will be sent and with what status? | request text and sha, chosen status, proxy and endpoint options, grant lifetime | SEND node: text sha, chars, status, proxy, endpoint, grant lasts; after Send: post, readback, transition |
| 10 | Does the reply promise, reverse or escalate wrongly? | voice rules (timeline commitment, dashes, escalation phrase), thread check silent reversals and re-asks | these are ISSUES with a sentence target, like any other |

A draft the operator cannot clear on all ten inside the pane is a draft the interface has failed.

## 3. The object model: the proof tree

One tree per draft. Nodes have an id, a one-line label, a mark, a body shown when expanded, children
(built on demand), and an optional zoom target (another node this one is about). The tree is built by
`desk.py tree --md5 M [--node ID] [--depth N]` from the record, and rendered two ways from the same JSON:
the Review pane, and `/desk show M [ID]` as text (the route for a session without a pane, and for anyone
reading over the shoulder). The model is testable in Python with no terminal.

```
draft a21f140c for #147802                      (root: ticket, md5, state, filed, by, supersedes)
|- ISSUES (3 open)                              first, always; empty reads "nothing open"
|  |- objection revB: "the second paragraph asserts the connector retries; the receipt says it reconnects"
|  |     state open   -> zooms to REPLY sentence 4
|  |- HOLD uncovered: sentence 6 has no receipt  -> sentence 6
|  '- thread recorded 41 min ago; ticket updated 12 min ago  -> ASK
|- ASK                                          the need
|  |- open questions since our last reply (1): "which agent version is required?"
|  |- the ask as thread-first read it: ...       (investigator summary)
|  '- THREAD 7 of 7 comments recorded 41 min ago
|     |- customer 10-14 09:12: ...               full text, yellow
|     '- us 10-14 15:40: ...
|- REPLY                                        the text, sentence by sentence
|  |- [docs+material] "The connector reports unhealthy when the broker is disabled."
|  |  |- receipt r1 docs docs.wiz.io/... (fetched page): "When the broker is disabled the connector ..."
|  |  |     minted by inv1 (sonnet) 10-15 18:44; reviewer B: says what the sentence says
|  |  |     '- the page, around the quote (expand)
|  |  |- receipt r4 material thread 10-14 09:12: "our broker was turned off on Monday"
|  |  |- carried by: evidence f3 (inv2), hypothesis f1 (inv1)
|  |  '- argued: counter c2 (adv) answered by a5 (drafter)
|  |- [docs] "Re-enable it under Settings > Connectors."
|  |- [none] "The connector retries every five minutes."   <- open objection revB, HOLD uncovered
|  '- [echo] "You mentioned the scan stopped on Monday."
|- DEBATE                                       the argument, by round
|  |- round 1: 2 objections (A: first paragraph; B: receipt fit), 2 answers
|  '- round 2: 1 objection open
|- RUN                                          who did what
|  |- inv1 investigator sonnet 2m14s completed: "..."  -> prompt (sha, 4.1k), answer (sha, 2.3k), 9 calls, 0 refused receipts
|  |- adv adversary opus ...
|  '- revB reviewer sonnet ...
'- SEND                                         what will happen
   |- text sha 3f9c..., 612 chars, status pending, zendesk-write / zendesk_update_ticket
   |- grant lasts 6 h; the gate refuses any other text, a second body, an internal note, a missing status
   '- after Send: the post, the readback, the transition (filled in as they happen)
```

Marks on a sentence: `[docs+material]` both legs, `[docs]`, `[material]`, `[search]` (a docs receipt checked
against a search result, not the page), `[none]` uncovered, `[echo]` restates the customer, `[problem]` with
the problem named. These are provenance facts, not scores: the desk does not grade prose, because scoring
has failed before and critique has worked. An objection is always a node of its own with a state.

Objection state: open (no answer finding about it), answered (an answer finding about it), overruled (the
operator sent with it open; the decision names it). The desk never closes an objection by itself.

## 4. Panes and keys

Three panes, the engine's own, switched by its tab bar or 1 to 3 from inside any pane:

- **1 Review**: the tree above for the selected draft, with the decisions.
- **2 Tickets**: the queue, one row per ticket; a row expands to its thread, its runs and its drafts.
- **3 Wall**: every agent live, one row each; Enter tails one (what 0.7.0 built).

Every pane: the second line reads `keys off: ctrl+x then tab, or click the pane` until it holds the
keyboard. Every key that does something is drawn as a labelled button on the pane (`z: zoom`, `u: up`); the
action rows ARE the key list, and nothing works that is not drawn. Tab and the arrows walk the rows, Enter
acts on the row under the ring. `f: find` puts the cursor in the field (type, Enter narrows to matching nodes
shown with their ancestors), `c: clear` empties it.

Review pane keys:

```
enter  expand or collapse the node under the ring
z      zoom: the node becomes the root, the breadcrumb grows (draft > REPLY > sentence 4 > receipt r1)
u      up one level of the breadcrumb
x      expand everything beneath the node under the ring
i      jump to ISSUES        t  jump to ASK and the thread        r  jump to REPLY
g      follow the zoom target of the node under the ring (an issue to its sentence, a sentence to its receipt)
n      next draft waiting    o  the ticket in Tickets
s      Send, pending   v  Send, solved   m  Modify (a line for the drafter)   d  Deny (why)
```

Send is the operator's call whatever ISSUES holds: a Send with issues open is recorded as an overrule naming
them, as today, and no reason is asked (decided 2026-10-15). The Send button's label says how many are open
(`s: Send, pending (2 open)`) so the press is informed.

Breadcrumb line, always first: `#147802  draft a21f140c  > REPLY > sentence 4` with the draft's state and age.

Wireframe, Review pane, 100 columns, zoomed to a sentence:

```
1: Review  2: Tickets  3: Wall     2 waiting, 0 agents, poll on
enter: open  z: zoom  u: up  x: all  i: issues  g: follow  s v m d: decide  f: find
find [                                  ]
#147802  draft a21f140c  clear  filed 18:51  > REPLY > sentence 1
[docs+material] The connector reports unhealthy when the broker is disabled.
- receipt r1  docs  https://docs.wiz.io/docs/connector-health  (fetched page)
    "When the broker is disabled, the connector reports unhealthy until it is re-enabled."
    minted by inv1 (sonnet) 18:44   reviewer B: says what the sentence says
    + the page around the quote
- receipt r4  material  customer 10-14 09:12
    "our broker was turned off on Monday for the firewall change"
    + the thread around the message
- carried by  evidence f3 (inv2)  hypothesis f1 (inv1)
    + f3: "Both the docs and the customer's own words put the broker ..."
- argued  counter c2 (adversary): "the customer may mean the message broker, not ours"
    answered by a5 (drafter): "the thread names the Wiz connector broker explicitly (r4)"
[ s Send, pending ]  v Send, solved   m Modify   d Deny   o ticket   n next
```

Tickets pane: rows `#n state SLA age subject`; Enter expands the row in place to `thread (n of m recorded,
age)`, `runs (phase, rounds, agents)`, `drafts (md5, marks, state)`; Enter on a draft opens it in Review;
`p` pulls the ticket under the ring; `r` re-reads the thread (asks the model to list the comments again, so
the record is fresh before a Send).

Wall pane: as 0.7.0.

## 5. What changes beneath

- `desk.py tree`: builds the node tree from what audit already walks, plus: objection state from `about`
  links and decisions; legs per sentence from claim classes and receipt kinds; the page excerpt around a
  docs quote from the pages store; the thread message around a material quote; staleness from the thread
  record and the queue row. Pure function of the record; unit tests per node type.
- `desk.py grant --overrule "<why>"`: the reason stored on the decision with the ids of the issues open at
  the press; `audit --text` prints it under the decision.
- `/desk show M [node]`: the tree as text, same JSON.
- The mod: one `ui.render` per pane; the Review pane holds expand state and the breadcrumb in `$.state`;
  a `ui.focus` hook keeps the ring per pane so `z`, `x`, `g`, `p` act on the row under it.
- Issue targets: every ISSUES child carries a node id; `g` zooms there.

Not changed: the gate, the grant, the record, the agents, the checks. The desk's rules stay where they are;
the interface shows their output with its reasons.

## 6. Build order (done in 0.8.0, in this order)

1. `desk.py tree` and `/desk show` with tests. The model proves itself as text before any pane draws it.
2. The Review pane on the tree: expand, zoom, up, follow, find, the decisions moved there.
3. Tickets pane: row expansion, re-read.
4. Retire the 0.7.0 Ticket and Decide panes.

## 7. Bars

- On a real draft the operator answers all ten questions in section 2 inside the pane, without `/desk
  audit` or the transcript.
- Every ISSUES child zooms to the sentence or node it is about; none is a bare string.
- `/desk show` and the pane agree (same JSON, one test renders both).
- Sends with open issues are counted as overrules in `desk stats`, with the issues named.
- Time from the draft filed to the decision, and the share decided as filed, are read from the ledger as
  before; the interface is judged on whether the second rises and the first falls.

## 8. Decisions for the operator

1. Three panes (Review, Tickets, Wall) as above, or one Review pane with Tickets and Wall as nodes of the
   tree. Recommendation: three; the Wall is a different rhythm (watching) from the Review (reading).
2. A Send with open issues requires a one-line reason. Recommendation: yes; it is the one control in this
   design that changes behaviour rather than visibility, and it is cheap.
3. Provenance marks only, no grades. Recommendation: marks only.
