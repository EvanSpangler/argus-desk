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

REVERSAL = re.compile(
    r"\b(?:my|our|the)\s+(?:last|previous|earlier)\s+(?:message|reply|answer|note)\b"
    r"|\bi\s+was\s+wrong\b|\bscratch\s+that\b|\bto\s+correct\s+(?:myself|that)\b"
    r"|\bignore\s+what\s+i\s+(?:said|wrote)\b|\bcorrection\s*:", re.I)
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


def mechanical(thread, draft, now, policy=POLICY):
    msgs = thread["messages"]
    hits = cadence(msgs, now, policy)
    if any(m["who"] == "us" for m in msgs) and REVERSAL.search(draft or ""):
        hits.append("announced-reversal")
    if repeat_ask(msgs, draft):
        hits.append("repeat-ask")
    return hits


def customer_questions(messages):
    """The customer's questions since our last public reply: shown to the operator, never a hold by themselves."""
    return [q for m in since_last_send(messages) for q in asks(m["text"]) if "?" in q]
