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
  consume   stdin {candidates:[body text], what, status}       -> the send gate: spends an open grant
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
SENT_GRACE_S = 120                                    # our own post moves updated_at; a move within this of the Send is ours
DASH = re.compile("[–—]")


def _iso_epoch(s):
    """Epoch seconds of an ISO 8601 timestamp as Zendesk writes it (2026-10-15T01:00:00Z), else None."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
TIMELINE = re.compile(r"\b(shortly|soon|asap|as soon as possible|by (?:end of|eod|tomorrow|monday|tuesday|wednesday|thursday|friday)|within (?:the )?(?:hour|day|\d+ (?:hours|days)))\b", re.I)


def _chain_hash(prev, row):
    body = json.dumps({k: v for k, v in row.items() if k != "h"}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((prev + "\n" + body).encode("utf-8")).hexdigest()


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name))[:80] or "agent"


def sha_of(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()



def canon_url(source):
    """One key for one page: scheme dropped, host lowercased, anchor and query dropped, trailing slash and
    punctuation dropped, so 'docs.wiz.io/docs/x/' and 'https://docs.wiz.io/docs/x#top' are the same page."""
    u = str(source or "").strip().strip("<>").rstrip(".,;:)")
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = u.split("#", 1)[0].split("?", 1)[0]
    host, sep, path = u.partition("/")
    return (host.lower() + sep + path).rstrip("/")

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
        """Append one record, chained: `prev` is the previous row's hash, `h` is sha256 over prev plus the row's
        own bytes. A row edited, dropped or inserted later breaks every hash after it (`desk verify`)."""
        row = dict(row, ts=row.get("ts") or self.clock())
        prev = self._last_hash(path)
        row["prev"] = prev
        row["h"] = _chain_hash(prev, row)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def _last_hash(self, path):
        last = ""
        if os.path.exists(path):
            with open(path, "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            last = json.loads(line).get("h") or last
                        except ValueError:
                            pass
        return last

    def verify(self):
        """Walk every chained file under the root and report the first broken link in each."""
        out = {"ok": True, "files": {}}
        paths = [self.requests_path, self.decisions_path, self.transitions_path]
        runs = os.path.join(self.root, "runs")
        if os.path.isdir(runs):
            for t in sorted(os.listdir(runs)):
                for n in ("events.jsonl", "findings.jsonl", "calls.jsonl"):
                    paths.append(os.path.join(runs, t, n))
        for path in paths:
            if not os.path.exists(path):
                continue
            prev, n, bad = "", 0, None
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if "h" not in row:
                        bad = bad or "line %d: unchained (written before v0.4)" % i
                        prev = ""
                        continue
                    if row.get("prev", "") != prev or _chain_hash(prev, row) != row["h"]:
                        bad = "line %d: hash mismatch" % i
                        break
                    prev = row["h"]
                    n += 1
            rel = os.path.relpath(path, self.root)
            out["files"][rel] = {"rows": n, "ok": bad is None, "problem": bad}
            if bad and "unchained" not in bad:
                out["ok"] = False
        return out

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
        # 3. claims: two-leg receipts, LIVE since v0.3 (the shadow count let drafts with uncovered and echo
        # sentences through as "clear"; the operator judged that far below the fleet's bar). Every checkable
        # sentence needs a receipt that says what it says; a problem on any sentence is a HOLD with its code.
        c = CR.check(draft, ticket, self.receipts_path, datetime.fromtimestamp(now, timezone.utc), inbound=inbound)
        claims = [r for r in c["claims"] if "snippet" not in r["classes"]]
        out["claims"] = {"checkable": len(claims), "credited": c["credited"],
                         "uncovered": sum(1 for r in claims if "uncovered" in r["problems"]),
                         "echo": sum(1 for r in claims if r.get("echo")), "hits": c["hits"], "shadow": False}
        out["hits"] += [h for h in c["hits"] if h not in out["hits"]]
        # 4. voice: the operator's own lint rules (<root>/voice-rules.json, exported from the fleet's voice-lint;
        # a built-in floor when the file is absent). block is a HOLD voice:<id>, warn is a warning.
        voice = self._voice(draft)
        out["voice"] = voice
        out["hits"] += ["voice:" + v["id"] for v in voice if v["severity"] == "block"]
        out["warnings"] += ["voice:" + v["id"] for v in voice if v["severity"] == "warn"]
        out["verdict"] = "hold" if out["hits"] else "clear"
        return out

    # The floor that applies without the operator's exported rules: what the fleet blocks on every channel.
    # (Dashes are the `dash` lint above, not a voice rule, so one dash is one hit.)
    BUILTIN_VOICE = [
        {"id": "absolutely-right", "pattern": "you(?:'re| are) absolutely right", "severity": "block"},
        {"id": "hope-finds-you-well", "pattern": "hope this (?:email|message|note) finds you well", "severity": "block"},
        {"id": "template-placeholder", "pattern": "\\[(?:name|customer|date|link|insert[^\\]]*)\\]|<(?:name|customer|link)>", "severity": "block"},
        {"id": "ai-self-reference", "pattern": "\\b(?:as an ai|language model|i am an ai)\\b", "severity": "block"},
        {"id": "model-vendor-name", "pattern": "\\b(?:claude|anthropic|openai|chatgpt|gpt-?\\d|gemini|argus)\\b", "severity": "block"},
        {"id": "typed-sign-off", "pattern": "(?:^|\\n)\\s*(?:best regards|kind regards|regards|thanks|thank you|cheers|best)[,!.]?\\s*\\n\\s*\\S+\\s*$", "severity": "block"},
        {"id": "unconfirmed-wording", "pattern": "\\b(?:i suspect|unconfirmed|i believe this is|my guess)\\b", "severity": "warn"},
        {"id": "let-me-know-if", "pattern": "let me know if", "severity": "warn"},
        {"id": "dont-hesitate", "pattern": "don'?t hesitate", "severity": "warn"},
        {"id": "feel-free", "pattern": "feel free to", "severity": "warn"},
        {"id": "hope-this-helps", "pattern": "hope this helps", "severity": "warn"},
    ]

    def _voice_rules(self):
        path = os.path.join(self.root, "voice-rules.json")
        rules = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    rules = json.load(fh).get("rules")
            except (OSError, ValueError):
                rules = None
        out = []
        for r in rules or self.BUILTIN_VOICE:
            ch = r.get("channels") or ["all"]
            if not ({"all", "customer", "ticket"} & set(ch)):
                continue
            try:
                out.append((r["id"], r.get("severity", "warn"), re.compile(r["pattern"], re.IGNORECASE | re.MULTILINE)))
            except (re.error, KeyError):
                continue
        return out

    def _voice(self, draft):
        hits = []
        for rid, sev, rx in self._voice_rules():
            if sev not in ("block", "warn"):
                continue
            m = rx.search(draft)
            if m:
                hits.append({"id": rid, "severity": sev, "at": draft[max(0, m.start() - 20):m.end() + 20].replace("\n", " ")})
        return hits

    # ---- commands
    def file(self, ticket, draft, source="argus", supersedes=None, by="argus"):
        text = _norm_draft(draft)
        md5 = md5_of(text)
        if not str(ticket).strip():
            return {"ok": False, "reason": "ticket is required"}
        if not text.strip():
            return {"ok": False, "reason": "draft is empty"}
        st = self.ledger.state(md5)
        if st["state"] in ("pending", "open"):
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
        """The drafts waiting for a decision. A request undecided past the ledger's REQUEST_TTL_S (24 h) and
        a grant past its 6 h life are written off first, so the desk shows what the guide says it shows."""
        self.ledger.sweep()
        events = self.ledger.events()
        items = []
        for md5, r in self._requests().items():
            st = self.ledger.state(md5, events=events)
            if st["state"] in ("pending", "open"):
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
            # The Send carries the status the posting call must set; the gate checks the call for it. A Send on a
            # draft the checks held is an overrule, named here so the audit shows what the operator waved through.
            overruled = list(req["checks"].get("hits") or [])
            self._append(self.transitions_path, {"md5": md5, "ticket": req["ticket"], "event": "granted", "status": status,
                                                 "overruled": overruled, "press_id": press_id})
            if overruled:
                self.event(req["ticket"], "operator", "overrule", "Send on HOLD %s" % ",".join(overruled))
        return dict(r, md5=md5, status=status, overruled=list(req["checks"].get("hits") or []))

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
        if st["state"] not in ("pending", "open"):
            return {"ok": False, "reason": "not undecided: " + st["state"]}
        self.ledger._append({"event": "withdraw", "md5": md5, "by": OPERATOR, "reason": "modify: " + (note or "")})
        self._decide(md5, req, "modify", note)
        return {"ok": True, "md5": md5, "ticket": req["ticket"], "note": note or ""}

    def _decide(self, md5, req, decision, note):
        now = self.clock()
        self._append(self.decisions_path, {"md5": md5, "ticket": req["ticket"], "decision": decision, "note": (note or "")[:500],
                                           "filed": req["ts"], "wait_s": int(now - req["ts"]), "verdict": req["checks"]["verdict"],
                                           "hits": req["checks"]["hits"], "overruled": req["checks"]["hits"] if decision == "send" else [],
                                           "source": req.get("source"),
                                           "supersedes": req.get("supersedes"), "edit": req.get("edit")})

    def _required_status(self, md5):
        for row in reversed(self._rows(self.transitions_path)):
            if row.get("md5") == md5 and row.get("event") == "granted":
                return row.get("status")
        return None

    def consume(self, candidates, what, status=None):
        """The send gate. `candidates` are the comment bodies the outward call carries (the mod extracts them
        from the body-like fields, nothing else). One of them must have an open grant, every other body in the
        call must be that same text, and `status` (the ticket status the call sets) must be the one the Send
        chose. Anything else is refused with the grant left open."""
        tried = []
        events = self.ledger.events()
        bodies = [c for c in (candidates or []) if isinstance(c, str)]
        for cand in bodies:
            for text in {cand, _norm_draft(cand)}:
                md5 = md5_of(text)
                st = self.ledger.state(md5, events=events)["state"]
                if st == "unknown":
                    continue
                tried.append({"md5": md5, "state": st})
                if st == "open":
                    others = [b for b in bodies if _norm_draft(b) != _norm_draft(text)]
                    if others:
                        return {"ok": False, "code": "body-mismatch", "md5": md5, "tried": tried,
                                "reason": "the call carries %d other body text(s) besides the granted one" % len(others)}
                    need = self._required_status(md5)
                    if need and status != need:
                        return {"ok": False, "code": "status-missing", "md5": md5, "status": need, "given": status, "tried": tried}
                    r = self.ledger.consume(md5, text, "argus", what, via="gate")
                    if r.get("ok"):
                        ticket = self._requests().get(md5, {}).get("ticket")
                        self._append(self.transitions_path, {"md5": md5, "ticket": ticket, "event": "posted", "status": need})
                        if ticket:
                            self._mark(ticket, "sent", md5=md5, status_set=need)
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
            if phase == "new" and (run.get("agents") or run.get("rounds")):
                # a ticket run again (after a Deny, or by hand): the earlier run is kept as history and the
                # counters start over, so the new run gets its own review rounds
                run.setdefault("history", []).append({"phase": run.get("phase"), "rounds": run.get("rounds", 0),
                                                      "started": run.get("started"), "ended": self.clock(), "agents": run.get("agents", [])})
                run.update(agents=[], rounds=0, started=self.clock())
            run["phase"], run["phase_at"] = phase, self.clock()
            if phase == "review":
                run["rounds"] = run.get("rounds", 0) + 1
        if agent and agent.get("id"):
            agent = dict(agent)
            # the exact prompt the agent was given and its full answer: kept whole, on disk, hashed in the row
            for key, sub in (("prompt", "prompts"), ("answer", "answers")):
                body = agent.pop(key, None)
                if body is not None:
                    d = os.path.join(self._run_dir(ticket), sub)
                    os.makedirs(d, exist_ok=True)
                    name = "%s.txt" % _safe(agent.get("label") or agent["id"])
                    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                        fh.write(str(body))
                    agent[key + "_sha"] = sha_of(body)
                    agent[key + "_file"] = os.path.join("runs", str(ticket), sub, name)
                    agent[key + "_chars"] = len(str(body))
            rows = [a for a in run["agents"] if a["id"] != agent["id"]]
            prev = next((a for a in run["agents"] if a["id"] == agent["id"]), {})
            if not prev and not agent.get("role"):
                # an update for an agent this run never spawned (a stray turn.complete): kept, but named for what it is
                agent.setdefault("role", "unknown")
                agent.setdefault("label", agent["id"][:24])
            row = dict(prev, **{k: v for k, v in agent.items() if v is not None})
            row.setdefault("started", self.clock())
            if row.get("status") in ("completed", "failed", "killed"):
                row.setdefault("ended", self.clock())
            rows.append(row)
            run["agents"] = sorted(rows, key=lambda a: a["started"])
        self._save_run(run)
        return {"ok": True, "run": run}

    def call(self, ticket, agent, tool, args, result=None, ok=True, note=None):
        """One tool call an agent made, with its full arguments and a hash of what came back: the record that
        says what the agent actually read or did, not what it says it did."""
        res = "" if result is None else str(result)
        row = {"agent": agent or "", "tool": str(tool or ""), "args": args, "ok": bool(ok),
               "result_sha": sha_of(res) if res else None, "result_chars": len(res), "note": note}
        self._append(os.path.join(self._run_dir(ticket), "calls.jsonl"), row)
        return {"ok": True}

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
                # 2026-10-15 (receipt fault): the refusal names what IS captured, so an agent that wrote the URL another way
                # (no scheme, trailing slash, anchor) can see the mismatch instead of reading "not captured" as "not read".
                r = {"ok": False, "why": "page-not-captured", "source": source, "captured": self._pages_captured()}
            else:
                # page_origin says what the captured body is: the page itself (fetch), or a docs search result that
                # names this URL (search). The audit shows the difference; the quote check is the same.
                r = CR.mint_docs(self.receipts_path, ticket, source or "", quote, page["body"], [quote], now, extra={"page_origin": page.get("origin")})
        elif kind == "material":
            rec = self._thread(ticket)
            if rec is None:
                # 2026-10-15 (receipt fault): "quote-not-in-material" against an empty thread read as a verbatim quote being refused. A thread
                # nobody captured is its own answer: the comments read never reached the desk in this run.
                r = {"ok": False, "why": "no-thread-captured",
                     "hint": "no comments read of #%s reached the desk in this run; read every comment through the proxy first (the desk records that read as the thread), then quote" % ticket}
            else:
                msgs = rec.get("thread", {}).get("messages", [])
                cust = [m for m in msgs if m.get("who") == "customer"]
                inbound = "\n".join(m.get("text", "") for m in cust)
                r = CR.mint_material(self.receipts_path, ticket, quote, inbound, [quote], now)
                if not r.get("ok"):
                    q = CR.norm(CR.plain(quote))
                    elsewhere = [m.get("who") for m in msgs if m.get("who") != "customer" and q and q in CR.norm(CR.plain(m.get("text", "")))]
                    r = {"ok": False, "why": "quote-not-from-customer" if elsewhere else "quote-not-in-material",
                         "thread": {"messages": len(msgs), "customer_messages": len(cust), "customer_chars": len(inbound),
                                    "fetched_age_s": int(self.clock() - float(rec.get("thread", {}).get("fetched_at") or 0))}}
                    if elsewhere:
                        r["found_in"] = elsewhere[0]
        elif kind == "test":
            r = CR.mint_test(self.receipts_path, ticket, source or "", quote, "captured_text", [quote], now)
        else:
            return {"ok": False, "why": "kind must be docs, material or test"}
        if r.get("ok"):
            self._append(os.path.join(self._run_dir(ticket), "events.jsonl"), {"agent": by or "", "kind": "receipt", "text": "%s %s: %s" % (r["id"], kind, quote[:120])})
        else:
            # the refusal is on the record with its reason, whichever path (hook or MCP) asked
            self._append(os.path.join(self._run_dir(ticket), "events.jsonl"),
                         {"agent": by or "", "kind": "receipt-refused", "text": "%s %s: %s%s" % (kind, r.get("why") or r.get("reason"), quote[:120], (" (%s)" % source) if source else "")})
        return r

    def audit(self, md5=None, ticket=None):
        """The chain behind one reply, walked backwards: what went out, who pressed, what the checks said, each
        sentence and its receipt, each receipt's source and captured bytes, the agent that minted it, that agent's
        model, prompt and answer, and every call it made. Everything named is a file or a hash the reader can open."""
        reqs = self._requests()
        if not md5 and ticket:
            mine = [r for r in reqs.values() if r["ticket"] == str(ticket)]
            if not mine:
                return {"ok": False, "reason": "no draft filed on #%s" % ticket}
            md5 = mine[-1]["md5"]
        req = reqs.get(md5)
        if not req:
            return {"ok": False, "reason": "unknown md5"}
        ticket = req["ticket"]
        run = self._run(ticket)
        events = self._rows(os.path.join(self._run_dir(ticket), "events.jsonl"))
        calls = self._rows(os.path.join(self._run_dir(ticket), "calls.jsonl"))
        findings = self._rows(os.path.join(self._run_dir(ticket), "findings.jsonl"))
        minted_by = {}
        for e in events:
            if e.get("kind") == "receipt":
                minted_by[e["text"].split(" ", 1)[0]] = e.get("agent")
        agents = {a.get("label"): a for a in run.get("agents", [])}
        ledger = [e for e in self.ledger.events() if e.get("md5") == md5]
        trans = [t for t in self._rows(self.transitions_path) if t.get("md5") == md5]
        decision = next((d for d in reversed(self._rows(self.decisions_path)) if d.get("md5") == md5), None)
        rec = self._thread(ticket)
        inbound = "\n".join(m["text"] for m in (rec or {}).get("thread", {}).get("messages", []) if m.get("who") == "customer")
        c = CR.check(req["text"], ticket, self.receipts_path, datetime.fromtimestamp(self.clock(), timezone.utc), inbound=inbound)
        receipts = {r["id"]: r for r in CR.load(self.receipts_path) if r["ticket"] == str(ticket)}
        sentences = []
        for cl in c["claims"]:
            if "snippet" in cl.get("classes", []):
                continue
            rids = cl.get("receipts") or []
            status = "ok" if rids and not cl["problems"] and not cl.get("echo") else ("echo" if cl.get("echo") else ",".join(cl["problems"]) or "uncovered")
            sentences.append({"text": cl["text"], "status": status,
                              "receipts": [self._receipt_chain(receipts.get(r), minted_by.get(r), agents, ticket) for r in rids]})
        return {"ok": True, "md5": md5, "ticket": ticket, "filed": req["ts"], "by": req.get("source"), "supersedes": req.get("supersedes"),
                "text_sha": sha_of(req["text"]), "chars": req["chars"], "checks_at_filing": req["checks"],
                "decision": decision, "transitions": trans, "ledger": ledger, "sentences": sentences,
                "objections": [f for f in findings if f.get("kind") == "objection" and f.get("about") in (None, md5)],
                "agents": [self._agent_chain(a, calls, events) for a in run.get("agents", [])],
                "captures": self._capture_chain(ticket),
                "chain": self.verify()}

    def _receipt_chain(self, r, by, agents, ticket):
        if not r:
            return {"id": None, "problem": "receipt not on file"}
        out = {"id": r["id"], "kind": r.get("kind"), "source": r.get("source") or r.get("url"), "quote": r.get("quote"),
               "minted": r.get("minted") or r.get("ts"), "by": by, "model": (agents.get(by) or {}).get("model"),
               "prompt_sha": (agents.get(by) or {}).get("prompt_sha"), "page_origin": r.get("page_origin")}
        src = out["source"]
        if r.get("kind") == "docs" and src:
            p = self._page_file(src)
            out["captured"] = {"file": os.path.relpath(p, self.root), "sha": _file_sha(p), "origin": r.get("page_origin")} if os.path.exists(p) else None
        elif r.get("kind") == "material":
            p = os.path.join(self.root, "threads", "%s.json" % ticket)
            out["captured"] = {"file": os.path.relpath(p, self.root), "sha": _file_sha(p)} if os.path.exists(p) else None
        return out

    def _agent_chain(self, a, calls, events):
        mine = [c for c in calls if c.get("agent") == a.get("label")]
        return {"label": a.get("label"), "role": a.get("role"), "model": a.get("model"), "status": a.get("status"),
                "started": a.get("started"), "ended": a.get("ended"), "params": a.get("params"),
                "prompt": {"sha": a.get("prompt_sha"), "file": a.get("prompt_file"), "chars": a.get("prompt_chars")},
                "answer": {"sha": a.get("answer_sha"), "file": a.get("answer_file"), "chars": a.get("answer_chars")},
                "calls": len(mine), "reads": mine[:200],
                "receipts": sum(1 for e in events if e.get("kind") == "receipt" and e.get("agent") == a.get("label")),
                "findings": sum(1 for e in events if e.get("kind") in FINDING_KINDS and e.get("agent") == a.get("label"))}

    def _capture_chain(self, ticket):
        out = {}
        p = os.path.join(self.root, "threads", "%s.json" % ticket)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                rec = json.load(fh)
            out["thread"] = {"file": os.path.relpath(p, self.root), "sha": _file_sha(p),
                             "messages": len(rec.get("thread", {}).get("messages", [])),
                             "fetched_at": rec.get("thread", {}).get("fetched_at"), "console_count": (rec.get("receipt") or {}).get("count")}
        pages = os.path.join(self.root, "pages")
        if os.path.isdir(pages):
            out["pages"] = []
            for n in sorted(os.listdir(pages)):
                with open(os.path.join(pages, n), encoding="utf-8") as fh:
                    pg = json.load(fh)
                out["pages"].append({"file": os.path.join("pages", n), "source": pg.get("source"),
                                     "sha": _file_sha(os.path.join(pages, n)), "chars": len(pg.get("body", ""))})
        return out

    def page(self, source, body, origin="fetch"):
        """Keep the body an agent read under a documentation URL, so a docs receipt can be checked against it.
        origin: "fetch" when the body is that page, "search" when it is a docs search result that names the URL
        (the quote is then verified against the result, not the page; the receipt and the audit say so)."""
        if not source or not str(body or "").strip():
            return {"ok": False, "reason": "source and body"}
        os.makedirs(os.path.join(self.root, "pages"), exist_ok=True)
        with open(os.path.join(self.root, "pages", md5_of(canon_url(source)) + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"source": source, "body": str(body), "origin": origin if origin in ("fetch", "search") else "search", "ts": self.clock()}, fh, ensure_ascii=False)
        return {"ok": True, "source": source, "chars": len(str(body)), "origin": origin}

    def _page(self, source):
        # 2026-10-15 (receipt fault): keyed by the canonical URL (no scheme, no anchor or query, no trailing slash, host lowercased) so the
        # URL as the agent wrote it meets the URL as the search result named it; files keyed the old way still resolve.
        for key in (canon_url(source), str(source or "")):
            p = os.path.join(self.root, "pages", md5_of(key) + ".json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    rec = json.load(fh)
                return {"body": rec.get("body"), "origin": rec.get("origin") or "search"}
        return None

    def _page_file(self, source):
        """The page file for a URL: canonical key first, the exact string for files written before 0.6.4."""
        for key in (canon_url(source), str(source or "")):
            p = os.path.join(self.root, "pages", md5_of(key) + ".json")
            if os.path.exists(p):
                return p
        return os.path.join(self.root, "pages", md5_of(canon_url(source)) + ".json")

    def _pages_captured(self, limit=8):
        """The documentation URLs captured so far, newest first: what a docs receipt may name."""
        d = os.path.join(self.root, "pages")
        if not os.path.isdir(d):
            return []
        rows = []
        for n in os.listdir(d):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    pg = json.load(fh)
                rows.append((float(pg.get("ts") or 0), str(pg.get("source") or "")))
            except (OSError, ValueError):
                continue
        return [src for _, src in sorted(rows, reverse=True)[:limit] if src]

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
        bad, last_err = 0, None
        while True:
            # 2026-10-15: one unreadable poll (a torn run.json, a transient OS error) ended the wait with a traceback and
            # the run looked crashed while its agents ran on. A poll that fails is retried; only a persistent fault returns.
            try:
                run = self._run(ticket)
                bad = 0
            except (ValueError, OSError) as e:
                bad, last_err = bad + 1, "%s: %s" % (type(e).__name__, str(e)[:200])
                if bad >= 5:
                    return {"ok": False, "phase": None, "reason": "run record unreadable: " + last_err, "ticket": str(ticket)}
                time.sleep(poll_s)
                continue
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

    def _answered_since_send(self, row, updated):
        """A ticket we posted to counts as answered only when its updated_at moved past our own post: past the
        updated_at the readback recorded (sent_updated), or, with no readback, more than SENT_GRACE_S after the
        Send. Zendesk's search index lags, so a sent ticket that merely reappears unchanged stays `sent`."""
        if not updated:
            return False
        if row.get("sent_updated"):
            return updated > row["sent_updated"]
        ts, sent_at = _iso_epoch(updated), row.get("sent_at")
        return bool(ts and sent_at and ts > sent_at + SENT_GRACE_S)

    def sync(self, tickets):
        """Fold one poll of the open set into the queue. A ticket seen for the first time is `new`; one we
        posted to that comes back after leaving, or that the customer updated since the post, is `reopened`;
        one we posted to that left the open set is `done`, the status transition confirmed; one that left
        without a post is `gone`."""
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
            answered = row["state"] == "done" or (row["state"] == "sent" and self._answered_since_send(row, t.get("updated")))
            row.update({k: v for k, v in fields.items() if v is not None})
            if answered:
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


def _when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%MZ") if ts else "?"


def render_audit(a):
    L = ["AUDIT draft %s on #%s, filed %s by %s, %d chars, sha256 %s" % (a["md5"], a["ticket"], _when(a["filed"]), a["by"], a["chars"], a["text_sha"][:16])]
    if a.get("supersedes"):
        L.append("  supersedes %s" % a["supersedes"])
    ch = a["checks_at_filing"]
    L.append("  checks at filing: %s%s" % (ch["verdict"].upper(), (" " + ",".join(ch["hits"])) if ch["hits"] else ""))
    d = a.get("decision")
    if d:
        L.append("  decision: %s at %s after %ss by the operator%s%s" % (
            d["decision"].upper(), _when(d["ts"]), d["wait_s"], (", note: " + d["note"]) if d.get("note") else "",
            (", OVERRULED " + ",".join(d["overruled"])) if d.get("overruled") else ""))
    else:
        L.append("  decision: none yet (waiting at the desk)")
    for t in a.get("transitions", []):
        extra = " ".join("%s=%s" % (k, v) for k, v in t.items() if k not in ("ts", "event", "md5", "ticket", "prev", "h") and v not in (None, [], ""))
        L.append("  %s: %s %s" % (_when(t["ts"]), t["event"], extra))
    for e in a.get("ledger", []):
        if e.get("event") in ("consume", "grant", "deny", "withdraw", "request"):
            L.append("  ledger %s: %s %s" % (_when(e.get("ts")), e["event"], (e.get("what") or e.get("reason") or e.get("target") or "")[:100]))
    L.append("SENTENCES")
    for s_ in a["sentences"]:
        L.append("  [%s] %s" % (s_["status"], s_["text"][:200]))
        for r in s_["receipts"]:
            if not r.get("id"):
                L.append("      receipt: %s" % r.get("problem"))
                continue
            cap = r.get("captured")
            L.append("      %s %s by %s (%s, prompt %s): \"%s\"" % (r["id"], r["kind"], r.get("by") or "?", r.get("model") or "?",
                                                                   (r.get("prompt_sha") or "?")[:12], (r.get("quote") or "")[:160]))
            how = "captured as a docs search result naming this URL, not the page" if cap and cap.get("origin") == "search" else "captured"
            L.append("        source %s; %s %s" % (r.get("source"), how, ("%s sha %s" % (cap["file"], cap["sha"][:16])) if cap else "MISSING"))
    if a.get("objections"):
        L.append("OBJECTIONS ON RECORD")
        for f in a["objections"]:
            L.append("  %s (%s %s): %s" % (str(f.get("id", ""))[:12], f.get("role"), f.get("agent"), (f.get("text") or "")[:220]))
    L.append("AGENTS")
    for g in a["agents"]:
        L.append("  %s %s (%s) %s %s to %s: %d calls, %d receipts, %d findings; prompt %s %s; answer %s %s" % (
            g["role"], g["label"], g.get("model") or "?", g.get("status"), _when(g.get("started")), _when(g.get("ended")),
            g["calls"], g["receipts"], g["findings"], (g["prompt"]["sha"] or "?")[:12], g["prompt"]["file"] or "(not kept)",
            (g["answer"]["sha"] or "?")[:12], g["answer"]["file"] or "(not kept)"))
        for c in g["reads"][:12]:
            L.append("      %s %s %s -> %s chars sha %s" % (_when(c.get("ts")), c.get("tool"), json.dumps(c.get("args"), ensure_ascii=False)[:140],
                                                          c.get("result_chars"), (c.get("result_sha") or "")[:12]))
        if len(g["reads"]) > 12:
            L.append("      ... %d more calls in runs/%s/calls.jsonl" % (len(g["reads"]) - 12, a["ticket"]))
    cp = a.get("captures", {})
    L.append("CAPTURES")
    if cp.get("thread"):
        t = cp["thread"]
        L.append("  thread %s: %s messages, console count %s, fetched %s, sha %s" % (t["file"], t["messages"], t["console_count"], _when(t.get("fetched_at")), t["sha"][:16]))
    for pg in cp.get("pages", []):
        L.append("  page %s: %s, %d chars, sha %s" % (pg["file"], pg["source"], pg["chars"], pg["sha"][:16]))
    v = a.get("chain", {})
    bad = [k for k, f in v.get("files", {}).items() if not f["ok"]]
    L.append("RECORD INTEGRITY: %s" % ("every chained file verifies" if v.get("ok") and not bad else "PROBLEM in " + ", ".join("%s (%s)" % (k, v["files"][k]["problem"]) for k in bad)))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["file", "list", "grant", "deny", "modify", "consume", "capture", "stats", "sync", "queue", "mark", "run", "event", "finding", "receipt", "page", "cite", "trace", "wait", "call", "audit", "verify"])
    ap.add_argument("--root", default=os.path.expanduser(os.environ.get("ARGUS_DESK_ROOT", "~/.argus-desk")))
    ap.add_argument("--md5")
    ap.add_argument("--press-id")
    ap.add_argument("--reason", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--status", default="pending", help="status the granted post must set")
    ap.add_argument("--ticket")
    ap.add_argument("--state")
    ap.add_argument("--updated", help="mark: the ticket's updated_at as read back after the post")
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
        out = d.consume(body.get("candidates") or [], body.get("what") or "", body.get("status"))
    elif a.cmd == "sync":
        out = d.sync(body.get("tickets") or [])
    elif a.cmd == "queue":
        out = d.queue_view()
    elif a.cmd == "mark":
        out = {"ok": True, "row": d._mark(a.ticket, a.state, note=a.note or None, sent_updated=a.updated or None)} if a.ticket and a.state else {"ok": False, "reason": "--ticket and --state"}
    elif a.cmd == "run":
        out = d.run_update(body.get("ticket"), body.get("phase"), body.get("agent"))
    elif a.cmd == "call":
        out = d.call(body.get("ticket"), body.get("agent"), body.get("tool"), body.get("args"), body.get("result"), body.get("ok", True), body.get("note"))
    elif a.cmd == "audit":
        out = d.audit(a.md5, a.ticket)
        if a.text and out.get("ok"):
            print(render_audit(out))
            return
    elif a.cmd == "verify":
        out = d.verify()
    elif a.cmd == "event":
        out = d.event(body.get("ticket"), body.get("agent"), body.get("kind"), body.get("text"))
    elif a.cmd == "finding":
        out = d.finding(body.get("ticket"), body.get("agent"), body.get("role"), body.get("kind"), body.get("text"), body.get("receipts"), body.get("about"))
    elif a.cmd == "receipt":
        out = d.receipt(body.get("ticket"), body.get("kind"), body.get("quote"), body.get("source"), body.get("by"))
    elif a.cmd == "page":
        out = d.page(body.get("source"), body.get("body"), body.get("origin") or "fetch")
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
