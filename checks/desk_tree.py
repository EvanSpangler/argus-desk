"""The proof tree behind one draft: the answer and everything that backs or disputes it, as nodes the operator
expands. Built from the record alone (the rows desk.py keeps), rendered two ways from the same JSON: the Review
pane, and `desk.py tree --text`. Marks are provenance facts (which legs carry a sentence); never grades.

Nodes: {"id", "label", "children", and when they apply "body" (shown expanded), "mark", "color", "state",
"target" (another node this one is about), "body_file" + "chars" (a body loaded on demand with --node)}.
"""
import os, re
from datetime import datetime, timezone

import claim_receipts as CR
import confirm_ledger as CL

BODY_CAP = 60_000
WORDS = re.compile(r"[a-z0-9]+")


def _n(id, label, children=None, **kw):
    out = {"id": id, "label": label}
    out.update({k: v for k, v in kw.items() if v is not None and v != []})
    out["children"] = children or []
    return out


def _when(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts)[:16]


def _ago(ts, now):
    if not ts:
        return "never"
    s = max(0, int(now - float(ts)))
    return "%dm" % (s // 60) if s < 36000 else "%dh" % (s // 3600)


def _dur(a):
    if not a.get("started"):
        return ""
    end = a.get("ended") or a.get("started")
    s = int(float(end) - float(a["started"]))
    return "%dm%02ds" % (s // 60, s % 60)


def _words(s):
    return WORDS.findall((s or "").lower())


def _shares_run(a, b, n=4):
    """True when a and b share n consecutive words: the objection quotes the sentence."""
    wa, wb = _words(a), _words(b)
    if len(wa) < n or len(wb) < n:
        return False
    grams = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    return any(tuple(wa[i:i + n]) in grams for i in range(len(wa) - n + 1))


def _excerpt(body, quote, around=240):
    if not body:
        return None
    nb, nq = CR.norm(body), CR.norm(quote or "")
    i = nb.find(nq) if nq else -1
    if i < 0:
        return body[:2 * around]
    # the normalised offset is close to the raw one for prose; widen the window so the quote is inside it
    start, end = max(0, i - around), min(len(body), i + len(nq) + around)
    return ("..." if start else "") + body[start:end] + ("..." if end < len(body) else "")


def _legs(receipts):
    legs = []
    for r in receipts:
        k = r.get("kind")
        leg = "search" if k == "docs" and r.get("page_origin") == "search" else k
        if leg and leg not in legs:
            legs.append(leg)
    order = {"docs": 0, "search": 1, "material": 2, "test": 3}
    return sorted(legs, key=lambda l: order.get(l, 9))


def build(desk, md5=None, ticket=None, send=None):
    """The whole tree for one draft (the latest on `ticket` when `md5` is not given)."""
    now = desk.clock()
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    reqs = desk._requests()
    if not md5 and ticket:
        mine = [r for r in reqs.values() if r["ticket"] == str(ticket)]
        if not mine:
            return {"ok": False, "reason": "no draft filed on #%s" % ticket}
        md5 = mine[-1]["md5"]
    req = reqs.get(md5)
    if not req:
        return {"ok": False, "reason": "unknown md5"}
    ticket = req["ticket"]
    rd = desk._run_dir(ticket)
    run = desk._run(ticket)
    findings = desk._rows(os.path.join(rd, "findings.jsonl"))
    events = desk._rows(os.path.join(rd, "events.jsonl"))
    calls = desk._rows(os.path.join(rd, "calls.jsonl"))
    receipts = {r["id"]: r for r in CR.load(desk.receipts_path) if r["ticket"] == str(ticket)}
    minted_by = {}
    for e in events:
        if e.get("kind") == "receipt":
            minted_by[e["text"].split(" ", 1)[0]] = e.get("agent")
    agents = {a.get("label"): a for a in run.get("agents", [])}
    rec = desk._thread(ticket) or {}
    thread = rec.get("thread", {})
    msgs = thread.get("messages", [])
    count = (rec.get("receipt") or {}).get("count")
    inbound = "\n".join(m["text"] for m in msgs if m.get("who") == "customer")
    check = CR.check(req["text"], ticket, desk.receipts_path, now_dt, inbound=inbound)
    claims = {CR.norm(c["text"]): c for c in check["claims"]}
    qrow = desk._queue().get(str(ticket), {})
    state = desk.ledger.state(md5)["state"]
    decision = next((d for d in reversed(desk._rows(desk.decisions_path)) if d.get("md5") == md5), None)
    transitions = [t for t in desk._rows(desk.transitions_path) if t.get("md5") == md5]
    ledger = [e for e in desk.ledger.events() if e.get("md5") == md5]
    filed_checks = req.get("checks") or {}
    thread_checks = filed_checks.get("thread") or {}

    # ---- REPLY: every sentence, its legs, its receipts, what carries it, what argued against it
    sentences = []
    for kind, u in CR.units(req["text"]):
        c = claims.get(CR.norm(u))
        rids = list(dict.fromkeys((c or {}).get("receipts") or []))   # a receipt bound twice (two citations) is one receipt
        rs = [receipts[r] for r in rids if r in receipts]
        legs = _legs(rs)
        problems = list((c or {}).get("problems") or [])
        if c is None:
            mark, st = "-", "asserts-nothing"
        elif c.get("echo"):
            mark, st = "echo", "echo"
        elif not rids:
            mark, st = "none", "uncovered"
        elif problems:
            mark, st = "+".join(legs) or "none", "problem"
        else:
            mark, st = "+".join(legs), "ok"
        sentences.append({"text": u, "mark": mark, "state": st, "problems": problems, "rids": rids, "receipts": rs, "classes": list((c or {}).get("classes") or [])})

    def locate(text, rids=()):
        for i, s in enumerate(sentences):
            if any(r in s["rids"] for r in rids) or _shares_run(text, s["text"]):
                return i
        return None

    by_id = {f.get("id"): f for f in findings}
    answers = {}
    for f in findings:
        if f.get("kind") == "answer" and f.get("about"):
            answers.setdefault(f["about"], []).append(f)
    objections = [f for f in findings if f.get("kind") == "objection" and not re.search(r"NO OBJECTIONS", f.get("text", ""), re.I)]
    counters = [f for f in findings if f.get("kind") == "counter"]

    def obj_state(f):
        if answers.get(f.get("id")):
            return "answered"
        if decision and decision.get("decision") == "send":
            return "overruled"
        return "open"

    def finding_node(f, prefix):
        kids = [_n("%s:%s" % (prefix, a.get("id")), "answer %s (%s): %s" % (a.get("id"), a.get("agent"), a.get("text", "")), body=a.get("text")) for a in answers.get(f.get("id"), [])]
        st = obj_state(f) if f.get("kind") in ("objection", "counter") else None
        loc = locate(f.get("text", ""), f.get("receipts") or [])
        return _n("%s:%s" % (prefix, f.get("id")),
                  "%s %s (%s)%s: %s" % (f.get("kind"), f.get("id"), f.get("agent"), (" [%s]" % st) if st else "", f.get("text", "")),
                  kids, body=f.get("text"), state=st, color="yellow" if st == "open" else None,
                  target=("reply:s%d" % loc) if loc is not None else None)

    reply_kids = []
    for i, s in enumerate(sentences):
        sid = "reply:s%d" % i
        kids = []
        for r in s["receipts"]:
            rid = r["id"]
            short = rid.replace("rcpt:", "")
            src = r.get("source") or r.get("url")
            origin = " (search result, not the page)" if r.get("kind") == "docs" and r.get("page_origin") == "search" else (" (fetched page)" if r.get("kind") == "docs" else "")
            by = minted_by.get(rid)
            model = (agents.get(by) or {}).get("model")
            rk = []
            if r.get("kind") == "docs" and src:
                page = desk._page(src)
                if page:
                    rk.append(_n("%s:r:%s:page" % (sid, short), "the page around the quote", body=_excerpt(page.get("body"), r.get("quote"))))
            elif r.get("kind") == "material":
                hit = next((m for m in msgs if CR.norm(r.get("quote", "")) and CR.norm(r.get("quote", "")) in CR.norm(m.get("text", ""))), None)
                if hit:
                    rk.append(_n("%s:r:%s:message" % (sid, short), "%s %s, the whole message" % (hit.get("who"), _when(hit.get("at"))), body=hit.get("text"), color="yellow" if hit.get("who") == "customer" else None))
            kids.append(_n("%s:r:%s" % (sid, short), 'receipt %s %s %s%s: "%s"' % (short, r.get("kind"), src or "thread", origin, r.get("quote", "")), rk,
                           body="minted by %s%s %s" % (by or "?", (" (%s)" % model) if model else "", (r.get("minted") or "")[:16]), target="run:%s" % by if by else None))
        carried = [f for f in findings if f.get("kind") in ("hypothesis", "evidence", "summary") and set(f.get("receipts") or []) & set(s["rids"])]
        if carried:
            kids.append(_n(sid + ":carried", "carried by %d finding(s)" % len(carried), [finding_node(f, sid + ":carried") for f in carried]))
        argued = [f for f in counters + objections if f.get("about") in {c.get("id") for c in carried} or locate(f.get("text", ""), f.get("receipts") or []) == i]
        if argued:
            kids.append(_n(sid + ":argued", "argued against by %d" % len(argued), [finding_node(f, sid + ":argued") for f in argued], color="yellow" if any(obj_state(f) == "open" for f in argued) else None))
        label = "[%s] %s%s" % (s["mark"], s["text"], (" !" + ",".join(s["problems"])) if s["problems"] else "")
        reply_kids.append(_n(sid, label, kids, mark=s["mark"], state=s["state"], body=s["text"],
                             color="yellow" if s["state"] in ("uncovered", "problem") else None))
    reply = _n("reply", "REPLY: %d sentences, %d receipted, %d uncovered, %d echo" % (
        len(sentences), sum(1 for s in sentences if s["state"] == "ok"), sum(1 for s in sentences if s["state"] == "uncovered"), sum(1 for s in sentences if s["state"] == "echo")), reply_kids)

    # ---- ASK: the need, the thread, its freshness
    ask_kids = []
    oq = thread_checks.get("open_questions") or []
    ask_kids.append(_n("ask:questions", "open questions since our last reply: %d" % len(oq),
                       [_n("ask:questions:%d" % i, q, body=q, color="yellow") for i, q in enumerate(oq)]))
    reader = next((a for a in run.get("agents", []) if "thread" in str(a.get("params") or "").lower() and a.get("summary")), None)
    if reader:
        ask_kids.append(_n("ask:summary", "the ask as %s read it: %s" % (reader.get("label"), reader.get("summary", "")), body=reader.get("summary"), target="run:%s" % reader.get("label")))
    fetched = thread.get("fetched_at")
    updated = qrow.get("updated")
    upd_ts = None
    if updated:
        try:
            upd_ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
        except ValueError:
            upd_ts = None
    stale = bool(fetched and upd_ts and upd_ts > float(fetched))
    tl = "THREAD: %s of %s comments recorded %s ago%s" % (len(msgs), count if count is not None else "?", _ago(fetched, now), "; TICKET UPDATED SINCE THE RECORD" if stale else "") if msgs else "THREAD: not recorded (the model has not read the comments on this ticket)"
    ask_kids.append(_n("ask:thread", tl, [_n("ask:thread:%s" % m.get("id", i), "%s %s: %s" % (m.get("who"), _when(m.get("at")), m.get("text", "")), body=m.get("text"), color="yellow" if m.get("who") == "customer" else None) for i, m in enumerate(msgs)],
                       state="stale" if stale else ("recorded" if msgs else "missing"), color="red" if stale else None))
    ask = _n("ask", "ASK: %d open question(s), thread %s" % (len(oq), "recorded" if msgs else "NOT recorded"), ask_kids)

    # ---- ISSUES: everything open, each pointing at what it is about
    issues = []
    for f in objections:
        if obj_state(f) == "open":
            loc = locate(f.get("text", ""), f.get("receipts") or [])
            issues.append(_n("issue:%s" % f.get("id"), "objection %s (%s): %s" % (f.get("id"), f.get("agent"), f.get("text", "")), body=f.get("text"), target=("reply:s%d" % loc) if loc is not None else "debate", color="yellow"))
    for i, s in enumerate(sentences):
        for p in s["problems"]:
            issues.append(_n("issue:s%d:%s" % (i, p), "%s: sentence %d, %s" % (p, i + 1, s["text"]), body=s["text"], target="reply:s%d" % i, color="yellow"))
        if s["state"] == "uncovered":
            issues.append(_n("issue:s%d:uncovered" % i, "uncovered: sentence %d has no receipt, %s" % (i + 1, s["text"]), body=s["text"], target="reply:s%d" % i, color="yellow"))
    for h in thread_checks.get("mechanical") or []:
        issues.append(_n("issue:thread:%s" % h, "thread check: %s" % h, target="ask:thread", color="yellow"))
    for w in filed_checks.get("warnings") or []:
        issues.append(_n("issue:warn:%s" % w, "warning at filing: %s" % w, target="ask", color="yellow"))
    for l in filed_checks.get("lint") or []:
        issues.append(_n("issue:voice:%s" % str(l)[:40], "voice rule: %s" % l, target="reply", color="yellow"))
    if stale:
        issues.append(_n("issue:stale", "the ticket was updated after the thread was recorded: read it again before Send", target="ask:thread", color="red"))
    issues_node = _n("issues", "ISSUES: %d open" % len(issues) if issues else "ISSUES: nothing open", issues, color="yellow" if issues else "green")

    # ---- DEBATE: the argument
    debate_kids = [finding_node(f, "debate") for f in findings if f.get("kind") in ("hypothesis", "counter", "objection")]
    debate = _n("debate", "DEBATE: %d hypotheses, %d counters, %d objections (%d open), rounds %s" % (
        sum(1 for f in findings if f.get("kind") == "hypothesis"), len(counters), len(objections), sum(1 for f in objections if obj_state(f) == "open"), run.get("rounds", 0)), debate_kids)

    # ---- RUN: who did what
    run_kids = []
    for a in run.get("agents", []):
        label = a.get("label") or a.get("id")
        mine = [c for c in calls if c.get("agent") == label]
        refused = [e for e in events if e.get("agent") == label and e.get("kind") == "receipt-refused"]
        glyph = {"completed": "v", "failed": "x", "running": "*"}.get(a.get("status"), ".")
        kids = []
        for key in ("prompt", "answer"):
            if a.get(key + "_file"):
                kids.append(_n("run:%s:%s" % (label, key), "%s: %s chars, sha %s" % (key, a.get(key + "_chars"), (a.get(key + "_sha") or "")[:12]), body_file=a[key + "_file"], chars=a.get(key + "_chars")))
        ck = []
        for i, c in enumerate(mine[:200]):
            args = c.get("args") or {}
            what = " ".join(str(args.get(k)) for k in ("proxy", "endpoint") if isinstance(args, dict) and args.get(k)) or str(args)[:80]
            ck.append(_n("run:%s:calls:%d" % (label, i), "%s %s%s" % (c.get("tool"), what, "" if c.get("ok", True) else " FAILED"), body=str(args)[:2000], color=None if c.get("ok", True) else "red"))
        kids.append(_n("run:%s:calls" % label, "%d calls, %d refused receipts" % (len(mine), len(refused)), ck + [
            _n("run:%s:refused:%d" % (label, i), "refused receipt: %s" % e.get("text", ""), body=e.get("text"), color="red") for i, e in enumerate(refused)]))
        run_kids.append(_n("run:%s" % label, "%s %s %s (%s) %s %s%s" % (glyph, a.get("role"), label, a.get("model", "?"), _dur(a), a.get("status", ""), (": " + a.get("summary", "")) if a.get("summary") else ""), kids,
                          body="\n".join(x for x in [a.get("summary") or "", ("angle: " + str(a.get("params"))) if a.get("params") else ""] if x), color="red" if a.get("status") == "failed" else None))
    run_node = _n("run", "RUN: phase %s, %d agents, rounds %s" % (run.get("phase", "none"), len(run.get("agents", [])), run.get("rounds", 0)), run_kids)

    # ---- SEND: what will happen, what happened
    send = send or {}
    status_set = next((t.get("status") for t in reversed(transitions) if t.get("event") == "granted"), None)
    send_kids = [
        _n("send:text", "text sha256 %s, %d chars, md5 %s" % (CR.hashlib.sha256(req["text"].encode()).hexdigest()[:16], req.get("chars", len(req["text"])), md5), body=req["text"]),
        _n("send:status", "status it sets: %s" % (status_set or "%s (chosen at Send; v sends as solved)" % send.get("status", "pending"))),
    ]
    if send.get("via"):
        send_kids.append(_n("send:via", "via %s" % send["via"]))
    send_kids.append(_n("send:grant", "a grant lasts %d h; the gate refuses any other text, a second body, an internal note, or a missing status" % (CL.GRANT_TTL_S // 3600)))
    if decision:
        send_kids.append(_n("send:decision", "%s at %s after %ss%s%s" % (decision.get("decision", "").upper(), _when(decision.get("ts")), decision.get("wait_s", 0),
                                                                        (", note: " + decision["note"]) if decision.get("note") else "",
                                                                        (", OVERRULED " + ",".join(decision["overruled"])) if decision.get("overruled") else ""), color="cyan"))
    for i, t in enumerate(transitions):
        send_kids.append(_n("send:t%d" % i, "%s%s%s" % (t.get("event"), (" status " + t["status"]) if t.get("status") else "", (" overruling " + ",".join(t["overruled"])) if t.get("overruled") else "")))
    for i, e in enumerate(ledger):
        send_kids.append(_n("send:l%d" % i, "ledger: %s by %s%s" % (e.get("event"), e.get("by"), (" via " + str(e.get("via"))) if e.get("via") else ""), body=e.get("what") or e.get("reason")))
    send_node = _n("send", "SEND: %s" % state, send_kids)

    root = _n("root", "draft %s for #%s  %s  filed %s by %s%s" % (md5[:8], ticket, state, _when(req.get("ts")), req.get("source", "?"), (", supersedes " + req["supersedes"][:8]) if req.get("supersedes") else ""),
              [issues_node, ask, reply, debate, run_node, send_node])
    return {"ok": True, "md5": md5, "ticket": ticket, "state": state, "open": len(issues), "tree": root}


def find(node, id):
    if node.get("id") == id:
        return node
    for c in node.get("children", []):
        hit = find(c, id)
        if hit:
            return hit
    return None


def load_body(desk, node):
    """A node whose body lives in a file (an agent's prompt or answer) gets it on demand, capped."""
    if node.get("body_file") and node.get("body") is None:
        p = os.path.join(desk.root, node["body_file"])
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                node["body"] = fh.read(BODY_CAP)
    return node


def render(node, depth=0, out=None, max_depth=99):
    """The tree as indented text, every node one line, bodies under their node."""
    out = [] if out is None else out
    pad = "  " * depth
    out.append("%s%s %s" % (pad, "-" if node.get("children") else "·", node.get("label", "")))
    body = str(node.get("body") or "").strip()
    if body and depth > 0 and body not in node.get("label", ""):
        for line in body.splitlines()[:40]:
            out.append("%s    %s" % (pad, line))
    if depth < max_depth:
        for c in node.get("children", []):
            render(c, depth + 1, out, max_depth)
    return out
