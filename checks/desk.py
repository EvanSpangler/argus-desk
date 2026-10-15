#!/usr/bin/env python3
"""argus-desk: the review desk's state and checks, stdlib only.

One process per call, invoked by the Claude Code mod through $.process.run. All state lives under
--root (default ~/.argus-desk): ledger.jsonl (positive-confirmation ledger, append-only),
requests.jsonl (checks per filed draft), threads/<ticket>.json (captured live thread + receipt),
receipts.jsonl (claim receipts), decisions.jsonl (operator decisions with timing, for the economics).

Subcommands (JSON in on stdin where noted, JSON out on stdout, exit 0 unless the call itself failed):
  file      stdin {ticket, draft, source, supersedes?, by?}   -> files a request after running checks
  list                                                        -> pending/open items with their checks
  grant     --md5 --press-id                                   -> operator pressed Send
  deny      --md5 --reason                                     -> operator pressed Deny
  modify    --md5 --note                                       -> operator asked for a change (returned)
  consume   stdin {candidates:[text], what}                    -> the send gate: spends an open grant
  capture   stdin {ticket, messages:[{id,who,at,text}], count} -> records the live thread + receipt
  stats                                                        -> the economics table
"""
import argparse, hashlib, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm_ledger as CL
import claim_receipts as CR
import thread_review as TR

OPERATOR = "operator"
DASH = re.compile("[–—]")
TIMELINE = re.compile(r"\b(shortly|soon|asap|as soon as possible|by (?:end of|eod|tomorrow|monday|tuesday|wednesday|thursday|friday)|within (?:the )?(?:hour|day|\d+ (?:hours|days)))\b", re.I)


def md5_of(text):
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _norm_draft(text):
    return (text or "").replace("\r\n", "\n").strip() + "\n"


class Desk:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = root, clock
        os.makedirs(os.path.join(root, "threads"), exist_ok=True)
        self.ledger = CL.Ledger(os.path.join(root, "ledger.jsonl"), approvers=(OPERATOR,), clock=clock)
        self.requests_path = os.path.join(root, "requests.jsonl")
        self.receipts_path = os.path.join(root, "receipts.jsonl")
        self.decisions_path = os.path.join(root, "decisions.jsonl")

    # ---- storage helpers
    def _rows(self, path):
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out

    def _append(self, path, row):
        row = dict(row, ts=row.get("ts") or self.clock())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def _thread(self, ticket):
        p = os.path.join(self.root, "threads", "%s.json" % ticket)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def _requests(self):
        return {r["md5"]: r for r in self._rows(self.requests_path)}

    # ---- checks
    def checks(self, ticket, draft):
        now = self.clock()
        out = {"hits": [], "warnings": []}
        # 1. lint the operator's standing rules (cheap, mechanical)
        lint = []
        if DASH.search(draft):
            lint.append("dash")
        if TIMELINE.search(draft):
            lint.append("timeline-commitment")
        out["lint"] = lint
        out["hits"] += lint
        # 2. thread: capture proof, cadence, reversal, repeat-ask, open customer questions
        rec = self._thread(ticket)
        if rec:
            thread, receipt = rec["thread"], rec["receipt"]
            cap = TR.capture_check(thread, receipt, now)
            mech = TR.mechanical(thread, draft, now)
            qs = TR.customer_questions(thread["messages"])
            out["thread"] = {"status": "captured", "messages": len(thread["messages"]), "console_count": receipt["count"],
                             "capture": cap, "mechanical": mech, "open_questions": qs,
                             "fetched_age_s": int(now - thread.get("fetched_at", 0))}
            out["hits"] += cap + mech
            inbound = "\n".join(m["text"] for m in thread["messages"] if m.get("who") == "customer")
        else:
            out["thread"] = {"status": "no-thread"}
            out["warnings"].append("no-thread-captured")
            inbound = ""
        # 3. claims: two-leg receipts. Shadow in v0: Argus has no receipt minting yet, so this is a count, not a hold.
        c = CR.check(draft, ticket, self.receipts_path, now, inbound=inbound)
        claims = [r for r in c["claims"] if "snippet" not in r["classes"]]
        out["claims"] = {"checkable": len(claims), "credited": c["credited"],
                         "uncovered": sum(1 for r in claims if "uncovered" in r["problems"]),
                         "echo": sum(1 for r in claims if r.get("echo")), "hits": c["hits"], "shadow": True}
        if c["hits"]:
            out["warnings"].append("claims:" + ",".join(c["hits"]))
        out["verdict"] = "hold" if out["hits"] else "clear"
        return out

    # ---- commands
    def file(self, ticket, draft, source="argus", supersedes=None, by="argus"):
        text = _norm_draft(draft)
        md5 = md5_of(text)
        if not str(ticket).strip():
            return {"ok": False, "reason": "ticket is required"}
        if not text.strip():
            return {"ok": False, "reason": "draft is empty"}
        st = self.ledger.state(md5)
        if st["state"] in ("pending", "open", "held"):
            return {"ok": False, "reason": "already filed and undecided", "md5": md5}
        checks = self.checks(str(ticket), text)
        target = "Zendesk #%s public reply" % ticket
        r = self.ledger.request(md5, text, target, by, kind="send")
        if not r.get("ok"):
            return dict(r, md5=md5)
        prior = self._requests().get(supersedes) if supersedes else None
        row = {"md5": md5, "ticket": str(ticket), "source": source, "supersedes": supersedes,
               "chars": len(text), "checks": checks, "text": text}
        if prior:
            row["edit"] = {"from": supersedes, "chars_before": prior.get("chars"), "distance": _distance(prior.get("text", ""), text)}
        self._append(self.requests_path, row)
        return {"ok": True, "md5": md5, "ticket": str(ticket), "verdict": checks["verdict"], "hits": checks["hits"],
                "warnings": checks["warnings"], "claims": checks["claims"], "thread": checks["thread"]}

    def list(self):
        items = []
        for md5, r in self._requests().items():
            st = self.ledger.state(md5)
            if st["state"] in ("pending", "open", "held"):
                items.append({"md5": md5, "ticket": r["ticket"], "state": st["state"], "source": r.get("source"),
                              "filed": r["ts"], "age_s": int(self.clock() - r["ts"]), "chars": r.get("chars"),
                              "checks": r["checks"], "text": r["text"], "supersedes": r.get("supersedes")})
        items.sort(key=lambda i: i["filed"])
        return {"ok": True, "items": items}

    def grant(self, md5, press_id):
        now = self.clock()
        req = self._requests().get(md5)
        if not req:
            return {"ok": False, "reason": "unknown md5"}
        # The press is the operator's own act at the terminal; the harness, not the model, raised it.
        src = CL.DictSource({press_id: {"from_operator": True, "automated": False, "ts": now, "body": "go %s" % md5}})
        r = self.ledger.grant_by_message(md5, press_id, src, OPERATOR)
        if r.get("ok"):
            self._decide(md5, req, "send", press_id)
        return dict(r, md5=md5)

    def deny(self, md5, reason):
        req = self._requests().get(md5)
        if not req:
            return {"ok": False, "reason": "unknown md5"}
        r = self.ledger.deny(md5, OPERATOR, reason or "denied at the desk")
        if r.get("ok"):
            self._decide(md5, req, "deny", reason)
        return dict(r, md5=md5)

    def modify(self, md5, note):
        """The operator wants a change: the request is closed as withdrawn and the note travels to the drafter.
        The refiled draft names this md5 as `supersedes`, which records the edit pair."""
        req = self._requests().get(md5)
        if not req:
            return {"ok": False, "reason": "unknown md5"}
        st = self.ledger.state(md5)
        if st["state"] not in ("pending", "open", "held"):
            return {"ok": False, "reason": "not undecided: " + st["state"]}
        self.ledger._append({"event": "withdraw", "md5": md5, "by": OPERATOR, "reason": "modify: " + (note or "")})
        self._decide(md5, req, "modify", note)
        return {"ok": True, "md5": md5, "ticket": req["ticket"], "note": note or ""}

    def _decide(self, md5, req, decision, note):
        now = self.clock()
        self._append(self.decisions_path, {"md5": md5, "ticket": req["ticket"], "decision": decision, "note": (note or "")[:500],
                                           "filed": req["ts"], "wait_s": int(now - req["ts"]), "verdict": req["checks"]["verdict"],
                                           "hits": req["checks"]["hits"], "source": req.get("source"),
                                           "supersedes": req.get("supersedes"), "edit": req.get("edit")})

    def consume(self, candidates, what):
        """The send gate. Each candidate is a text the outward call carries; the first whose md5 has an open
        grant is consumed. Nothing else gets through."""
        tried = []
        for cand in candidates or []:
            if not isinstance(cand, str):
                continue
            for text in {cand, _norm_draft(cand)}:
                md5 = md5_of(text)
                st = self.ledger.state(md5)["state"]
                if st == "unknown":
                    continue
                tried.append({"md5": md5, "state": st})
                if st == "open":
                    r = self.ledger.consume(md5, text, "argus", what, via="gate")
                    if r.get("ok"):
                        return {"ok": True, "md5": md5}
        pend = [i["md5"] for i in self.list()["items"]]
        return {"ok": False, "tried": tried, "undecided": pend}

    def capture(self, ticket, messages, count):
        now = self.clock()
        msgs = []
        for i, m in enumerate(messages or []):
            msgs.append({"id": str(m.get("id") or i), "who": m.get("who") or "customer",
                         "at": float(m.get("at") or now), "text": str(m.get("text") or "")})
        rec = {"thread": {"id": str(ticket), "source": "live", "fetched_at": now, "messages": msgs},
               "receipt": {"thread": str(ticket), "count": int(count if count is not None else len(msgs)), "at": now}}
        with open(os.path.join(self.root, "threads", "%s.json" % ticket), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        return {"ok": True, "ticket": str(ticket), "messages": len(msgs), "count": rec["receipt"]["count"]}

    def stats(self):
        dec = self._rows(self.decisions_path)
        reqs = self._rows(self.requests_path)
        n = len(dec)
        by = {k: sum(1 for d in dec if d["decision"] == k) for k in ("send", "deny", "modify")}
        waits = sorted(d["wait_s"] for d in dec)
        sent_as_filed = sum(1 for d in dec if d["decision"] == "send" and not d.get("supersedes"))
        sent_after_edit = sum(1 for d in dec if d["decision"] == "send" and d.get("supersedes"))
        dist = sorted(d["edit"]["distance"] for d in dec if d.get("edit") and d["edit"].get("distance") is not None)
        holds = sum(1 for r in reqs if r["checks"]["verdict"] == "hold")
        hit_counts = {}
        for r in reqs:
            for h in r["checks"]["hits"]:
                hit_counts[h] = hit_counts.get(h, 0) + 1
        held_then = {k: sum(1 for d in dec if d["verdict"] == "hold" and d["decision"] == k) for k in ("send", "deny", "modify")}
        clear_then = {k: sum(1 for d in dec if d["verdict"] == "clear" and d["decision"] == k) for k in ("send", "deny", "modify")}
        return {"ok": True, "filed": len(reqs), "decided": n, "decisions": by,
                "sent_as_filed": sent_as_filed, "sent_after_edit": sent_after_edit,
                "wait_s": {"median": _pct(waits, 0.5), "p90": _pct(waits, 0.9)},
                "edit_distance": {"median": _pct(dist, 0.5), "n": len(dist)},
                "check_holds": holds, "hit_counts": hit_counts,
                "decision_by_verdict": {"hold": held_then, "clear": clear_then}}


def _pct(xs, p):
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def _distance(a, b):
    """Character edit distance on word tokens, bounded for long texts. A rough size of the operator's edit."""
    x, y = a.split(), b.split()
    if len(x) * len(y) > 250000:
        return abs(len(x) - len(y))
    prev = list(range(len(y) + 1))
    for i, xi in enumerate(x, 1):
        cur = [i]
        for j, yj in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (xi != yj)))
        prev = cur
    return prev[-1]


def stats_text(s):
    lines = ["argus-desk stats", "filed %d, decided %d (send %d, modify %d, deny %d)" % (
        s["filed"], s["decided"], s["decisions"]["send"], s["decisions"]["modify"], s["decisions"]["deny"])]
    if s["decided"]:
        lines.append("sent as filed %d, sent after an edit %d, denied %d" % (s["sent_as_filed"], s["sent_after_edit"], s["decisions"]["deny"]))
        lines.append("wait to decision: median %ss, p90 %ss" % (s["wait_s"]["median"], s["wait_s"]["p90"]))
    if s["edit_distance"]["n"]:
        lines.append("edit distance on refiled drafts: median %s words (n=%d)" % (s["edit_distance"]["median"], s["edit_distance"]["n"]))
    lines.append("checks held %d of %d filed; hits: %s" % (s["check_holds"], s["filed"], ", ".join("%s %d" % kv for kv in sorted(s["hit_counts"].items())) or "none"))
    lines.append("decisions on held drafts: %s; on clear drafts: %s" % (
        ", ".join("%s %d" % kv for kv in s["decision_by_verdict"]["hold"].items()),
        ", ".join("%s %d" % kv for kv in s["decision_by_verdict"]["clear"].items())))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["file", "list", "grant", "deny", "modify", "consume", "capture", "stats"])
    ap.add_argument("--root", default=os.path.expanduser(os.environ.get("ARGUS_DESK_ROOT", "~/.argus-desk")))
    ap.add_argument("--md5")
    ap.add_argument("--press-id")
    ap.add_argument("--reason", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--text", action="store_true", help="stats as text")
    a = ap.parse_args(argv)
    # the mod passes its stateDir option as given, often "~/.argus-desk"; no shell expands it on the way here
    d = Desk(os.path.expanduser(a.root))
    body = {}
    if a.cmd in ("file", "consume", "capture"):
        raw = sys.stdin.read()
        body = json.loads(raw) if raw.strip() else {}
    if a.cmd == "file":
        out = d.file(body.get("ticket", ""), body.get("draft", ""), body.get("source", "argus"), body.get("supersedes"), body.get("by", "argus"))
    elif a.cmd == "list":
        out = d.list()
    elif a.cmd == "grant":
        out = d.grant(a.md5, a.press_id or ("press-%d" % int(time.time() * 1000)))
    elif a.cmd == "deny":
        out = d.deny(a.md5, a.reason)
    elif a.cmd == "modify":
        out = d.modify(a.md5, a.note)
    elif a.cmd == "consume":
        out = d.consume(body.get("candidates") or [], body.get("what") or "")
    elif a.cmd == "capture":
        out = d.capture(body.get("ticket"), body.get("messages"), body.get("count"))
    else:
        out = d.stats()
        if a.text:
            print(stats_text(out))
            return 0
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
