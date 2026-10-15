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
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm_ledger as CL
import claim_receipts as CR
import thread_review as TR

OPERATOR = "operator"
# Official documentation hosts a docs receipt may cite, on top of claim_receipts' own list: ARGUS_DESK_DOCS_HOSTS=a,b
for _h in os.environ.get("ARGUS_DESK_DOCS_HOSTS", "").split(","):
    if _h.strip() and _h.strip() not in CR.POLICY["docs_hosts"]:
        CR.POLICY["docs_hosts"].append(_h.strip())
FINDING_KINDS = ("hypothesis", "evidence", "counter", "objection", "answer", "summary")
STATUSES = ("pending", "solved", "open", "on-hold")   # the Zendesk status a Send may set
STALE_S = 30 * 60                                     # a pull with nothing filed after this is offered again
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
        self.transitions_path = os.path.join(root, "transitions.jsonl")
        self.queue_path = os.path.join(root, "queue.json")
        os.makedirs(os.path.join(root, "runs"), exist_ok=True)

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
        c = CR.check(draft, ticket, self.receipts_path, datetime.fromtimestamp(now, timezone.utc), inbound=inbound)
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

    def grant(self, md5, press_id, status="pending"):
        now = self.clock()
        req = self._requests().get(md5)
        if not req:
            return {"ok": False, "reason": "unknown md5"}
        if status not in STATUSES:
            return {"ok": False, "reason": "status must be one of %s" % ", ".join(STATUSES)}
        # The press is the operator's own act at the terminal; the harness, not the model, raised it.
        src = CL.DictSource({press_id: {"from_operator": True, "automated": False, "ts": now, "body": "go %s" % md5}})
        r = self.ledger.grant_by_message(md5, press_id, src, OPERATOR)
        if r.get("ok"):
            self._decide(md5, req, "send", press_id)
            # The Send carries the status the posting call must set; the gate checks the call for it.
            self._append(self.transitions_path, {"md5": md5, "ticket": req["ticket"], "event": "granted", "status": status})
        return dict(r, md5=md5, status=status)

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

    def _required_status(self, md5):
        for row in reversed(self._rows(self.transitions_path)):
            if row.get("md5") == md5 and row.get("event") == "granted":
                return row.get("status")
        return None

    def consume(self, candidates, what, flat=""):
        """The send gate. Each candidate is a text the outward call carries; the first whose md5 has an open
        grant is consumed. Nothing else gets through. `flat` is the whole call as JSON: the post must also
        set the status the Send chose, or it is refused with the grant left open."""
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
                    need = self._required_status(md5)
                    if need and not re.search(r'"status"\s*:\s*"%s"' % re.escape(need), flat or ""):
                        return {"ok": False, "code": "status-missing", "md5": md5, "status": need, "tried": tried}
                    r = self.ledger.consume(md5, text, "argus", what, via="gate")
                    if r.get("ok"):
                        ticket = self._requests().get(md5, {}).get("ticket")
                        self._append(self.transitions_path, {"md5": md5, "ticket": ticket, "event": "posted", "status": need})
                        if ticket:
                            self._mark(ticket, "sent", md5=md5, status=need)
                        return {"ok": True, "md5": md5, "ticket": ticket, "status": need}
        pend = [i["md5"] for i in self.list()["items"]]
        return {"ok": False, "tried": tried, "undecided": pend}

    # ---- runs: one investigation per ticket, a tree of agents, everything they read and said
    def _run_dir(self, ticket):
        d = os.path.join(self.root, "runs", str(ticket))
        os.makedirs(d, exist_ok=True)
        return d

    def _run(self, ticket):
        p = os.path.join(self._run_dir(ticket), "run.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        return {"ticket": str(ticket), "started": self.clock(), "agents": [], "phase": "new", "rounds": 0}

    def _save_run(self, run):
        p = os.path.join(self._run_dir(run["ticket"]), "run.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(run, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return run

    def run_update(self, ticket, phase=None, agent=None):
        """Record a phase change or an agent's state. `agent` = {id, role, model, status, summary?, params?}."""
        run = self._run(ticket)
        if phase:
            run["phase"], run["phase_at"] = phase, self.clock()
            if phase == "review":
                run["rounds"] = run.get("rounds", 0) + 1
        if agent and agent.get("id"):
            rows = [a for a in run["agents"] if a["id"] != agent["id"]]
            prev = next((a for a in run["agents"] if a["id"] == agent["id"]), {})
            row = dict(prev, **{k: v for k, v in agent.items() if v is not None})
            row.setdefault("started", self.clock())
            if row.get("status") in ("completed", "failed", "killed"):
                row.setdefault("ended", self.clock())
            rows.append(row)
            run["agents"] = sorted(rows, key=lambda a: a["started"])
        self._save_run(run)
        return {"ok": True, "run": run}

    def event(self, ticket, agent, kind, text):
        """One line of the tail: what an agent is doing right now. Kept short, kept all."""
        row = {"agent": agent, "kind": kind, "text": str(text or "")[:400]}
        self._append(os.path.join(self._run_dir(ticket), "events.jsonl"), row)
        return {"ok": True}

    def finding(self, ticket, agent, role, kind, text, receipts=None, about=None):
        """What an agent concluded: a hypothesis, evidence for or against, an objection, an answer to one."""
        if kind not in FINDING_KINDS:
            return {"ok": False, "reason": "kind must be one of %s" % ", ".join(FINDING_KINDS)}
        if not str(text or "").strip():
            return {"ok": False, "reason": "text is empty"}
        row = {"id": "f-" + md5_of("%s%s%s%s" % (ticket, agent, kind, text))[:8], "agent": agent, "role": role, "kind": kind,
               "text": str(text)[:2000], "receipts": [r for r in (receipts or []) if isinstance(r, str)], "about": about}
        self._append(os.path.join(self._run_dir(ticket), "findings.jsonl"), row)
        return {"ok": True, "id": row["id"]}

    def receipt(self, ticket, kind, quote, source=None, by=None):
        """Mint a receipt from something an agent actually read: `docs` (a page on an official host, the quote
        verbatim), `material` (the customer's own words, found in the captured thread), `test` (recorded output
        of a real run). The rules are claim_receipts'; a quote nobody read is refused."""
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        quote = str(quote or "").strip()
        if kind == "docs":
            page = self._page(source)
            if page is None:
                return {"ok": False, "why": "page-not-captured", "source": source}
            r = CR.mint_docs(self.receipts_path, ticket, source or "", quote, page, [quote], now)
        elif kind == "material":
            rec = self._thread(ticket)
            inbound = "\n".join(m["text"] for m in (rec or {}).get("thread", {}).get("messages", []) if m.get("who") == "customer")
            r = CR.mint_material(self.receipts_path, ticket, quote, inbound, [quote], now)
        elif kind == "test":
            r = CR.mint_test(self.receipts_path, ticket, source or "", quote, "captured_text", [quote], now)
        else:
            return {"ok": False, "why": "kind must be docs, material or test"}
        if r.get("ok"):
            self._append(os.path.join(self._run_dir(ticket), "events.jsonl"), {"agent": by or "", "kind": "receipt", "text": "%s %s: %s" % (r["id"], kind, quote[:120])})
        return r

    def page(self, source, body):
        """Keep the body of a page an agent fetched, so a docs receipt can be checked against it."""
        if not source or not str(body or "").strip():
            return {"ok": False, "reason": "source and body"}
        os.makedirs(os.path.join(self.root, "pages"), exist_ok=True)
        with open(os.path.join(self.root, "pages", md5_of(source) + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"source": source, "body": str(body), "ts": self.clock()}, fh, ensure_ascii=False)
        return {"ok": True, "source": source, "chars": len(str(body))}

    def _page(self, source):
        p = os.path.join(self.root, "pages", md5_of(source or "") + ".json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("body")

    def cite(self, ticket, citations):
        """Bind receipts to the sentences of a draft: each citation is {receipt, sentence}. A receipt the ticket
        does not hold is refused; the binding is a fresh row so the claim check finds the sentence in `bound`."""
        mine = {r["id"]: r for r in CR.load(self.receipts_path) if r["ticket"] == str(ticket)}
        out = {"ok": True, "bound": 0, "unknown": []}
        for c in citations or []:
            rid, sent = str(c.get("receipt") or ""), str(c.get("sentence") or "").strip()
            if rid not in mine or not sent:
                out["unknown"].append(rid)
                continue
            row = dict(mine[rid], bound=sorted(set(mine[rid].get("bound", [])) | {sent}), minted=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat())
            CR._append(self.receipts_path, row)
            out["bound"] += 1
        return out

    def wait(self, ticket, timeout_s=1800, poll_s=2.0):
        """Block until the run's phase is terminal; the mod calls this from a command so the turn holds."""
        deadline = time.time() + float(timeout_s)
        while True:
            run = self._run(ticket)
            if run.get("phase") in ("done", "failed"):
                return {"ok": True, "phase": run["phase"], "ticket": str(ticket)}
            if time.time() >= deadline:
                return {"ok": False, "phase": run.get("phase"), "reason": "timeout", "ticket": str(ticket)}
            time.sleep(poll_s)

    def trace(self, ticket):
        """Everything the operator reads before Send: the run, its agents, the findings, the receipts, the tail."""
        d = self._run_dir(ticket)
        run = self._run(ticket)
        findings = self._rows(os.path.join(d, "findings.jsonl"))
        events = self._rows(os.path.join(d, "events.jsonl"))
        receipts = [r for r in CR.load(self.receipts_path) if r["ticket"] == str(ticket)]
        seen, uniq = set(), []
        for r in reversed(receipts):
            if r["id"] not in seen:
                seen.add(r["id"]); uniq.append(r)
        drafts = [i for i in self.list()["items"] if i["ticket"] == str(ticket)]
        claims = []
        if drafts:
            c = CR.check(drafts[-1]["text"], ticket, self.receipts_path, datetime.fromtimestamp(self.clock(), timezone.utc),
                         inbound="\n".join(m["text"] for m in (self._thread(ticket) or {}).get("thread", {}).get("messages", []) if m.get("who") == "customer"))
            claims = c["claims"]
        return {"ok": True, "run": run, "findings": findings, "receipts": list(reversed(uniq)), "events": events[-200:],
                "claims": claims, "drafts": drafts}

    # ---- the queue: open tickets the poller saw, and where each one stands
    def _queue(self):
        if not os.path.exists(self.queue_path):
            return {}
        with open(self.queue_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _save_queue(self, q):
        tmp = self.queue_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(q, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.queue_path)

    def _mark(self, ticket, state, **extra):
        q = self._queue()
        row = q.setdefault(str(ticket), {"first_seen": self.clock(), "state": "new"})
        row["state"] = state
        row[state + "_at"] = self.clock()
        row.update({k: v for k, v in extra.items() if v is not None})
        self._save_queue(q)
        return row

    def sync(self, tickets):
        """Fold one poll of the open set into the queue. A ticket seen for the first time is `new`; one we
        posted to that reappears with a newer update is `reopened`; one we posted to that left the open set
        is `done`, the status transition confirmed; one that left without a post is `gone`."""
        now = self.clock()
        q = self._queue()
        seen = set()
        for t in tickets or []:
            tid = str(t.get("id") or "").strip()
            if not tid:
                continue
            seen.add(tid)
            row = q.get(tid)
            fields = {"status": t.get("status"), "sla": t.get("sla"), "subject": (t.get("subject") or "")[:120],
                      "updated": t.get("updated"), "last_seen": now}
            if not row:
                q[tid] = dict(fields, first_seen=now, state="new", new_at=now)
                continue
            row.update({k: v for k, v in fields.items() if v is not None})
            if row["state"] in ("sent", "done") and (row.get("updated") or "") > (row.get("sent_updated") or ""):
                row["state"], row["reopened_at"] = "reopened", now
            elif row["state"] == "gone":
                row["state"], row["new_at"] = "new", now
        for tid, row in q.items():
            if tid in seen or row["state"] in ("done", "gone", "skipped"):
                continue
            if row["state"] == "sent":
                row["state"], row["done_at"] = "done", now
                row["transition"] = "confirmed"
                self._append(self.transitions_path, {"md5": row.get("md5"), "ticket": tid, "event": "confirmed", "status": row.get("status_set")})
            else:
                row["state"], row["gone_at"] = "gone", now
        self._save_queue(q)
        return self.queue_view()

    def queue_view(self):
        now = self.clock()
        q = self._queue()
        waiting = {i["ticket"] for i in self.list()["items"]}
        rows = []
        for tid, row in q.items():
            st = row["state"]
            if tid in waiting:
                st = "waiting"
            elif st == "drafting" and now - row.get("drafting_at", now) > STALE_S:
                st = "stale"
            rows.append({"ticket": tid, "state": st, "sla": row.get("sla"), "subject": row.get("subject"),
                         "age_s": int(now - row.get("first_seen", now)), "status": row.get("status")})
        busy = [r for r in rows if r["state"] in ("drafting", "waiting")]
        pullable = [r for r in rows if r["state"] in ("new", "reopened", "stale")]
        pullable.sort(key=lambda r: (r["sla"] is None, r["sla"] or "", -r["age_s"]))
        order = {"waiting": 0, "drafting": 1, "stale": 2, "reopened": 3, "new": 4, "sent": 5, "skipped": 6, "done": 7, "gone": 8}
        rows.sort(key=lambda r: (order.get(r["state"], 9), r["sla"] is None, r["sla"] or "", -r["age_s"]))
        nxt = None if busy else (pullable[0] if pullable else None)
        return {"ok": True, "rows": rows, "next": nxt, "busy": [r["ticket"] for r in busy],
                "counts": {k: sum(1 for r in rows if r["state"] == k) for k in order}}

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
        tr = self._rows(self.transitions_path)
        posted = sum(1 for t in tr if t["event"] == "posted")
        confirmed = sum(1 for t in tr if t["event"] == "confirmed")
        q = self._queue()
        return {"ok": True, "filed": len(reqs), "decided": n, "decisions": by,
                "transitions": {"posted": posted, "confirmed": confirmed},
                "queue": {k: sum(1 for r in q.values() if r["state"] == k) for k in ("new", "drafting", "sent", "done", "gone", "skipped")},
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
    lines.append("posts through the gate %d, status transition confirmed by the next poll %d" % (s["transitions"]["posted"], s["transitions"]["confirmed"]))
    lines.append("queue: %s" % ", ".join("%s %d" % kv for kv in s["queue"].items()))
    lines.append("checks held %d of %d filed; hits: %s" % (s["check_holds"], s["filed"], ", ".join("%s %d" % kv for kv in sorted(s["hit_counts"].items())) or "none"))
    lines.append("decisions on held drafts: %s; on clear drafts: %s" % (
        ", ".join("%s %d" % kv for kv in s["decision_by_verdict"]["hold"].items()),
        ", ".join("%s %d" % kv for kv in s["decision_by_verdict"]["clear"].items())))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["file", "list", "grant", "deny", "modify", "consume", "capture", "stats", "sync", "queue", "mark", "run", "event", "finding", "receipt", "page", "cite", "trace", "wait"])
    ap.add_argument("--root", default=os.path.expanduser(os.environ.get("ARGUS_DESK_ROOT", "~/.argus-desk")))
    ap.add_argument("--md5")
    ap.add_argument("--press-id")
    ap.add_argument("--reason", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--status", default="pending", help="status the granted post must set")
    ap.add_argument("--ticket")
    ap.add_argument("--state")
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--text", action="store_true", help="stats as text")
    a = ap.parse_args(argv)
    # the mod passes its stateDir option as given, often "~/.argus-desk"; no shell expands it on the way here
    d = Desk(os.path.expanduser(a.root))
    body = {}
    if a.cmd in ("file", "consume", "capture", "sync", "run", "event", "finding", "receipt", "page", "cite"):
        raw = sys.stdin.read()
        body = json.loads(raw) if raw.strip() else {}
    if a.cmd == "file":
        out = d.file(body.get("ticket", ""), body.get("draft", ""), body.get("source", "argus"), body.get("supersedes"), body.get("by", "argus"))
    elif a.cmd == "list":
        out = d.list()
    elif a.cmd == "grant":
        out = d.grant(a.md5, a.press_id or ("press-%d" % int(time.time() * 1000)), a.status)
    elif a.cmd == "deny":
        out = d.deny(a.md5, a.reason)
    elif a.cmd == "modify":
        out = d.modify(a.md5, a.note)
    elif a.cmd == "consume":
        out = d.consume(body.get("candidates") or [], body.get("what") or "", body.get("flat") or "")
    elif a.cmd == "sync":
        out = d.sync(body.get("tickets") or [])
    elif a.cmd == "queue":
        out = d.queue_view()
    elif a.cmd == "mark":
        out = {"ok": True, "row": d._mark(a.ticket, a.state, note=a.note or None)} if a.ticket and a.state else {"ok": False, "reason": "--ticket and --state"}
    elif a.cmd == "run":
        out = d.run_update(body.get("ticket"), body.get("phase"), body.get("agent"))
    elif a.cmd == "event":
        out = d.event(body.get("ticket"), body.get("agent"), body.get("kind"), body.get("text"))
    elif a.cmd == "finding":
        out = d.finding(body.get("ticket"), body.get("agent"), body.get("role"), body.get("kind"), body.get("text"), body.get("receipts"), body.get("about"))
    elif a.cmd == "receipt":
        out = d.receipt(body.get("ticket"), body.get("kind"), body.get("quote"), body.get("source"), body.get("by"))
    elif a.cmd == "page":
        out = d.page(body.get("source"), body.get("body"))
    elif a.cmd == "cite":
        out = d.cite(body.get("ticket"), body.get("citations"))
    elif a.cmd == "trace":
        out = d.trace(a.ticket)
    elif a.cmd == "wait":
        out = d.wait(a.ticket, a.timeout)
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
