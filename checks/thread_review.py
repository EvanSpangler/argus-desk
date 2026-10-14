"""Reference decision rule: review a draft reply against the WHOLE conversation thread.

Stdlib only, no network. The model call is outside this file: a finder proposes findings as plain
dicts, optional verifier votes arrive as lists of labels, and everything here is deterministic.

Message shape: {"id": str, "who": "customer"|"us"|"staff", "at": epoch seconds, "text": str}
Thread shape:  {"id": str, "source": "live", "fetched_at": epoch seconds, "messages": [message]}
Receipt shape: {"thread": str, "count": int, "at": epoch seconds}   (count as the console shows it)
"""
import hashlib
import json
import os
import re

POLICY = {"receipt_max_age": 900, "min_gap_hours": 4.0, "window_hours": 12.0, "max_per_window": 2}

REVERSAL = re.compile(
    r"\b(?:my|our|the)\s+(?:last|previous|earlier)\s+(?:message|reply|answer|note)\b"
    r"|\bi\s+was\s+wrong\b|\bscratch\s+that\b|\bto\s+correct\s+(?:myself|that)\b"
    r"|\bignore\s+what\s+i\s+(?:said|wrote)\b|\bcorrection\s*:", re.I)
ATTRIBUTION = re.compile(
    r"\b(?:you\s+(?:said|mentioned|wrote|told|noted|confirmed)|as\s+(?:i|we)\s+(?:said|noted|mentioned|"
    r"discussed)|(?:my|our)\s+(?:last|previous|earlier)\s+\w+|earlier\s+(?:you|we|i)|per\s+your)\b", re.I)
ASK = re.compile(r"\?|\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:send|share|provide|confirm|attach|run)\b", re.I)
SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
WORD = re.compile(r"[a-z0-9']+")
KINDS = ("self-reversal", "consensus-conflict", "ignores-customer-info", "misattributed")


def norm(s):
    s = (s or "").replace("’", "'").replace("“", '"').replace("”", '"')
    return " ".join(s.lower().split())


def found(quote, text, min_chars=12):
    """A quote counts only if it is verbatim (modulo case and spacing) in the named text."""
    q = norm(quote)
    return len(q) >= min_chars and q in norm(text)


def _overlap(a, b):
    x, y = set(WORD.findall(norm(a))), set(WORD.findall(norm(b)))
    return len(x & y) / float(len(x | y)) if x and y else 0.0


def _digest(*parts):
    return hashlib.sha256("\x1f".join(norm(p) if i else p for i, p in enumerate(parts)).encode()).hexdigest()[:24]


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


def fingerprint(thread):
    return _digest("t", thread["id"], str(len(thread["messages"])), *[m["id"] + m["text"] for m in thread["messages"]])


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
    return [q for m in since_last_send(messages) for q in asks(m["text"]) if "?" in q]


# ---- anchoring: a finding survives only if every quote is really in the named text ----

def anchor(findings, thread, draft):
    texts = {w: "\n".join(m["text"] for m in thread["messages"] if m["who"] == w) for w in ("customer", "us", "staff")}
    out = []
    for f in findings or []:
        k = f.get("kind")
        ok = False
        if k == "self-reversal":
            ok = found(f.get("earlier"), texts["us"]) and found(f.get("draft_says"), draft)
        elif k == "consensus-conflict":
            ok = found(f.get("settled"), texts["staff"] + "\n" + texts["us"]) and found(f.get("draft_says"), draft)
        elif k == "ignores-customer-info":
            ok = found(f.get("customer_gave"), texts["customer"]) and found(f.get("draft_says"), draft)
        elif k == "misattributed":
            ok = (found(f.get("draft_says"), draft) and bool(ATTRIBUTION.search(f.get("draft_says") or ""))
                  and (not f.get("thread_says") or found(f.get("thread_says"), "\n".join(texts.values()))))
        if ok:
            f = dict(f, id=k + ":" + _digest("f", k, f.get("draft_says", ""), f.get("earlier", "") + f.get("settled", "")
                                              + f.get("customer_gave", "") + f.get("thread_says", ""))[:8])
            out.append(f)
    return out


def judge_questions(finder_qs, thread, draft):
    """Every customer question since our last send needs an anchored, non-echo answer; else it is open."""
    latest = "\n".join(m["text"] for m in since_last_send(thread["messages"]))
    mech = customer_questions(thread["messages"])
    open_q, seen = [], []
    for q in finder_qs or []:
        qt, st, ds = q.get("question", ""), q.get("status"), q.get("draft_sentence")
        if not found(qt, latest, 8):
            continue                         # invented question: dropped
        seen.append(qt)
        good = st in ("answered", "asked_back") and found(ds, draft, 8) and _overlap(ds, qt) < 0.8
        if not good:
            open_q.append(qt)
    for m in mech:                           # a question the finder skipped counts as unanswered
        if not any(_overlap(m, s) > 0.5 for s in seen):
            open_q.append(m)
    return open_q


# ---- verdict ----

def majority(votes):
    """Strict majority REAL stands; ties, empty and unreadable votes stand (fail toward the operator)."""
    v = [x for x in (votes or []) if x in ("REAL", "NOT_REAL")]
    return "NOT_REAL" if v and sum(1 for x in v if x == "NOT_REAL") * 2 > len(v) else "REAL"


def review(thread, receipt, draft, finder, votes, now, policy=POLICY):
    """finder = {"questions": [...], "findings": [...]}; votes = {finding id or question text: [labels]}."""
    held = capture_check(thread, receipt, now, policy)
    if held:                                 # an unproven thread is not reviewed at all
        return {"verdict": "hold", "hits": held, "warnings": [], "overruled": [], "fp": None}
    hits = mechanical(thread, draft, now, policy)
    warnings, overruled = [], []
    if len(thread["messages"]) > int(receipt["count"]):
        warnings.append("more-recorded-than-counted")
    for q in judge_questions(finder.get("questions"), thread, draft):
        if majority((votes or {}).get(q)) == "REAL":
            hits.append("question-not-answered")
        else:
            overruled.append("question:" + q[:40])
    for f in anchor(finder.get("findings"), thread, draft):
        if majority((votes or {}).get(f["id"])) == "REAL":
            hits.append(f["kind"])
        else:
            overruled.append(f["id"])
    hits = sorted(set(hits))
    if not hits and overruled:               # cleared only by overruling one of its own findings
        hits = ["cleared-by-overrule"]
    return {"verdict": "hold" if hits else "clear", "hits": hits, "warnings": warnings,
            "overruled": overruled, "fp": fingerprint(thread)}


# ---- tiny file-backed store: one verdict per (fresh thread, exact draft); operator releases ----

class Store:
    def __init__(self, path):
        self.path = path

    def _load(self):
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, self.path)

    @staticmethod
    def key(thread, draft):
        return _digest("k", fingerprint(thread), draft)

    def verdict(self, thread, draft, compute):
        """Reuse the stored verdict for this exact draft on this exact thread state; else compute once."""
        d, k = self._load(), self.key(thread, draft)
        if k not in d or d[k]["result"]["fp"] is None:     # a capture failure is never reused
            d[k] = {"result": compute(), "released_by": None}
            self._save(d)
        return d[k]

    def release(self, thread, draft, operator):
        d, k = self._load(), self.key(thread, draft)
        if not (operator and k in d) or d[k]["result"]["fp"] is None:
            return False                     # an unproven thread cannot be released, only re-captured
        d[k]["released_by"] = operator
        self._save(d)
        return True

    def may_send(self, thread, draft):
        r = self._load().get(self.key(thread, draft))
        return bool(r) and r["result"]["fp"] is not None and (r["result"]["verdict"] == "clear" or bool(r["released_by"]))
