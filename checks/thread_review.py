"""Decision rules that read a draft reply against the WHOLE captured conversation thread.

Stdlib only, no network, no model: everything here is deterministic over the captured bytes.

Message shape: {"id": str, "who": "customer"|"us"|"staff", "at": epoch seconds, "text": str}
Thread shape:  {"id": str, "source": "live", "fetched_at": epoch seconds, "messages": [message]}
Receipt shape: {"thread": str, "count": int, "at": epoch seconds}   (count as the console shows it)

What the desk uses: capture_check, mechanical, customer_questions. The model-assisted finder, anchor,
vote and store parts of the original reference implementation are not part of the desk and were
removed in v0.6 (git history has them).
"""
import re

POLICY = {"receipt_max_age": 900, "min_gap_hours": 4.0, "window_hours": 12.0, "max_per_window": 2}

# The fleet's thread-check REVERSAL, kept identical: a desk draft that clears here must clear there.
REVERSAL = re.compile(
    r"\b(?:my|the|our)\s+(?:last|previous|earlier|prior)\s+(?:message|reply|response|email|note|comment)\b"
    r"|\bi\s+(?:was|got\s+that)\s+wrong\b"
    r"|\bwas\s+wrong\s+to\s+(?:tell|say|suggest|leave|advise)\b"
    r"|\b(?:correction|apologies|sorry)[,:]?\s+(?:that|the|my|i)\b"
    r"|\bignore\s+(?:my|the)\s+(?:last|previous|earlier)\b"
    r"|\bto\s+correct\s+(?:myself|my\s+last)\b"
    r"|\bscratch\s+that\b"
    r"|\bignore\s+what\s+i\s+(?:said|wrote)\b|\bcorrection\s*:",
    re.I,
)
ASK = re.compile(r"\?|\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:send|share|provide|confirm|attach|run)\b", re.I)
SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


# ---- capture: the thread must be proven fresh and complete before any review ----

def capture_check(thread, receipt, now, policy=POLICY):
    """Return a list of hold codes. Empty means the thread is proven fresh and complete."""
    if not thread or thread.get("source") != "live":
        return ["thread-not-live"]          # a cached or reconstructed copy is never the thread
    if not receipt or receipt.get("thread") != thread.get("id"):
        return ["capture-missing"]
    age = now - receipt.get("at", 0)
    if age < 0 or age > policy["receipt_max_age"]:
        return ["capture-stale"]
    fetched = thread.get("fetched_at", 0)
    if now - fetched > policy["receipt_max_age"] or abs(fetched - receipt["at"]) > policy["receipt_max_age"]:
        return ["thread-stale"]
    have = len(thread.get("messages") or [])
    if have < int(receipt["count"]):
        return ["unrecorded-comment"]       # the console shows more than we hold: some were never read
    return []


# ---- mechanical checks (no model) ----

def asks(text):
    return [s.strip() for s in SPLIT.split(text or "") if s.strip() and ASK.search(s)]


def since_last_send(messages):
    last = max([i for i, m in enumerate(messages) if m["who"] == "us"], default=-1)
    return [m for m in messages[last + 1:] if m["who"] == "customer"]


def cadence(messages, now, policy=POLICY):
    ours = sorted(m["at"] for m in messages if m["who"] == "us")
    hits = []
    if ours and (now - ours[-1]) / 3600.0 < policy["min_gap_hours"]:
        hits.append("too-soon")
    if sum(1 for t in ours if now - t <= policy["window_hours"] * 3600) >= policy["max_per_window"]:
        hits.append("too-many")
    return hits


def repeat_ask(messages, draft):
    """Asking again while our latest ask is still unanswered (no customer message after it)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["who"] == "customer":
            return False
        if m["who"] == "us" and asks(m["text"]):
            return bool(asks(draft))
    return False


# customer-cannot-act, the fleet's reply-review rule kept identical (#143907 rejected, #144757 escalated: "asking directly for
# steps ... the response was aimed at checking our boxes"). When the customer's latest messages ask for steps, ask what we
# want, or say they are confused, the draft carries numbered steps or a plain imperative ask.
ASKS_STEPS = re.compile(r"\b(?:what(?:'s| is) your ask|what (?:do|would) you (?:want|need|like)(?: me| us)? to do|what (?:should|do) (?:i|we) do"
                        r"|(?:the |exact |detailed |clear )?steps?\b(?! (?:1|2|3|one|two|three)\b)[^.?!\n]{0,40}(?:\?|to (?:take|follow|run|reproduce|fix|resolve))"
                        r"|how (?:do|should|can|would) (?:i|we) (?:run|do|fix|resolve|configure|set|enable|apply|proceed)"
                        r"|walk (?:me|us) through|step[- ]by[- ]step|(?:i am|i'm|we are|we're) (?:confused|unclear|lost|not (?:sure|clear))"
                        r"|(?:not|isn't|is not) clear (?:to (?:me|us)|what)|(?:can|could) you (?:please )?(?:clarify|simplify|explain (?:simply|in simple|plainly|clearly))"
                        r"|do you want (?:me|us) to\b)", re.I)
HAS_STEPS = re.compile(r"(?m)^\s*(?:\d+[.)]|step \d+[:.)]?)\s+\S")
HAS_ASK = re.compile(r"(?m)^(?:please |could you |can you )?(?:run|add|remove|set|enable|disable|open|send|attach|scan|re-?run|change|use|keep|leave|update|apply|try|check|confirm|let me know)\b", re.I)


def cannot_act(latest, draft):
    text = " ".join(str(m.get("text") or "") for m in latest or [])
    if not ASKS_STEPS.search(text):
        return False
    steps, ask = len(HAS_STEPS.findall(draft or "")), HAS_ASK.search(draft or "")
    return not (steps >= 2 or (steps >= 1 and ask) or (ask and "?" not in (draft or "")))


# holding-reply, the fleet's F-34 in its mechanical form: the customer asked, the draft says we are still checking, and it
# names no action (a reproduction, an escalation, a filed bug, a test). The fleet's phrases, kept identical.
CHECKING = re.compile(
    r"\b(?:still (?:checking|confirming|verifying|looking into|working on|reviewing)|"
    r"(?:don'?t|do not) have (?:an?|the)?\s*[\w'-]*\s*answer\b.{0,20}?\byet\b|not sure yet|no answer yet|"
    r"haven'?t confirmed|need to (?:check|verify|confirm)|not (?:yet )?confirmed|"
    r"i'?m (?:checking|confirming|verifying|looking into|reviewing)|we'?re (?:checking|confirming|verifying|looking into|reviewing)|"
    r"(?:checking|confirming|verifying|looking into) (?:this|that|it) now|don'?t (?:yet )?know (?:yet|the answer))\b", re.I)
ACTION = re.compile(
    r"\b(?:reproduc\w+|escalat\w+|raised (?:this|it|the issue) with|filed (?:a|an|the)? ?(?:bug|ticket|request|wz)|WZ-\d+|"
    r"engineering (?:has|have|is|are|ticket|team)|with (?:our )?engineering|checking with (?:engineering|the team|product)|"
    r"asked (?:engineering|the team|product)|running (?:a |the )?(?:repro\w*|test|scan)|testing (?:this|it|that) (?:now|in|on|against))", re.I)


def mechanical(thread, draft, now, policy=POLICY):
    msgs = thread["messages"]
    hits = cadence(msgs, now, policy)
    latest = since_last_send(msgs)
    if cannot_act(latest, draft):
        hits.append("customer-cannot-act")
    if any("?" in q for m in latest for q in asks(m["text"])) and CHECKING.search(draft or "") and not ACTION.search(draft or ""):
        hits.append("holding-reply")
    if any(m["who"] == "us" for m in msgs) and REVERSAL.search(draft or ""):
        hits.append("announced-reversal")
    if repeat_ask(msgs, draft):
        hits.append("repeat-ask")
    return hits


def customer_questions(messages):
    """The customer's questions since our last public reply: shown to the operator, never a hold by themselves."""
    return [q for m in since_last_send(messages) for q in asks(m["text"]) if "?" in q]
