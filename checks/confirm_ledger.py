"""Positive-confirmation ledger: reference implementation (stdlib only).

An irreversible outward action may run only against an open grant. A grant exists only because the
operator, in a message the harness can authenticate, named the md5 of the exact text and said go.
The action consumes the grant once. Silence, old messages, and grants for other text never count.
Time is epoch seconds, injected through `clock` so tests are deterministic.
"""
import fcntl, hashlib, json, os, re, time
from contextlib import contextmanager

REQUEST_TTL_S = 24 * 3600      # a request nobody decided is swept
GRANT_TTL_S = 6 * 3600         # default life of a grant
MESSAGE_MAX_AGE_S = 15 * 60    # a message older than this cannot back a new grant
CLOCK_SKEW_S = 60
CHECK_GRACE_S = 30             # in-flight window for a check run inside the consuming command
YES = {"go", "approve", "approved", "yes", "send", "ok", "confirmed"}
NO = {"no", "not", "hold", "wait", "stop", "deny", "denied", "dont", "don't", "cancel", "revoke"}
CODES = {"open": 0, "unknown": 3, "expired": 4, "consumed": 5, "revoked": 6, "denied": 7,
         "withdrawn": 8, "pending": 9, "held": 11, "done": 12}

def md5_of(text):
    # An identifier for the exact bytes, not a security control; sha256 works the same way.
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()

def words_of(body):
    return set(re.findall(r"[a-z']+", str(body).lower()))

def hex_tokens(body):
    return re.findall(r"\b[0-9a-f]{8,32}\b", str(body).lower())

class DictSource:
    """Stand-in for the harness-owned record of operator messages. Real sources must be written by
    the transport layer, never by model output. Each record: from_operator, automated, ts, body."""

    def __init__(self, records=None):
        self.records = dict(records or {})

    def lookup(self, msg_id):
        return self.records.get(str(msg_id))

class LedgerCorrupt(Exception):
    """A readable line followed an unreadable one: the history cannot be trusted."""

def next_unanswered(messages, answered):
    """Answer credit is per message: the OLDEST message not yet answered is the one a reply spends.
    messages: list of (id, ts). Returns an id or None."""
    for mid, _ts in sorted(messages, key=lambda m: m[1]):
        if str(mid) not in answered:
            return str(mid)
    return None

class Ledger:
    def __init__(self, path, approvers=(), clock=time.time):
        self.path, self.approvers, self.clock = path, set(approvers), clock

    # ---- storage: append-only JSON lines, one write per event, lock around read-check-append
    @contextmanager
    def _lock(self):
        with open(self.path + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def events(self, strict=False):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        out = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                if strict and i != len(lines) - 1 and any(x.strip() for x in lines[i + 1:]):
                    raise LedgerCorrupt("unreadable line %d" % (i + 1))
        return out

    def _append(self, ev):
        ev = {"ts": self.clock(), **ev}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(ev, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return ev

    # ---- reading state
    def delegation(self, name, now=None):
        now = self.clock() if now is None else now
        last = None
        for ev in self.events():
            if ev.get("name") == name and ev["event"] in ("delegate", "delegate-revoke"):
                last = ev
        if last is None:
            return {"state": "none", "ev": None}
        if last["event"] == "delegate-revoke":
            return {"state": "revoked", "ev": last}      # revocation beats an unexpired delegation
        return {"state": "expired" if last["expires"] < now else "open", "ev": last}

    def state(self, md5, now=None, events=None):
        now = self.clock() if now is None else now
        grant = req = last = None
        for ev in (self.events() if events is None else events):
            if ev.get("md5") != md5 or ev["event"] == "use":
                continue
            last = ev
            if ev["event"] == "request":
                req = ev
            if ev["event"] == "grant":
                grant = ev
        if last is None:
            return {"state": "unknown", "grant": None, "request": None, "last": None}
        simple = {"request": "pending", "consume": "consumed", "done": "done", "revoke": "revoked",
                  "deny": "denied", "withdraw": "withdrawn", "expire": "expired"}
        out = {"state": simple.get(last["event"], "unknown"), "grant": grant, "request": req, "last": last}
        if last["event"] == "grant":
            out["state"] = "expired" if last["expires"] < now else "open"
            if out["state"] == "open" and last.get("under"):
                d = self.delegation(last["under"], now)       # re-judged at use time, not only at grant time
                scope = (d["ev"] or {}).get("classes", [])
                if d["state"] != "open" or (req or {}).get("kind") not in scope:
                    out.update(state="held", why="delegation %s" % d["state"] if d["state"] != "open" else "outside scope")
        return out

    def used_messages(self):
        return {str(e["msg"]) for e in self.events() if e["event"] in ("grant", "delegate") and e.get("msg")}

    def disposition(self, md5):
        """sent / pending / abandoned / unclear. Prose is never parsed; a revoke must say its outcome."""
        s = self.state(md5)
        st, last = s["state"], s["last"] or {}
        if st in ("consumed", "done"):
            return "sent"
        if st in ("pending", "open", "held"):
            return "pending"
        if st == "revoked":
            return {"sent": "sent", "abandoned": "abandoned"}.get(last.get("outcome"), "unclear")
        return "abandoned" if st in ("denied", "withdrawn", "expired") else "unclear"

    def stamp(self, md5):
        """Latest event marker for a message to carry; is_current() detects a crossed message."""
        evs = [e for e in self.events() if e.get("md5") == md5]
        return "%s@%.3f" % (evs[-1]["event"], evs[-1]["ts"]) if evs else "none"

    def is_current(self, md5, stamp):
        return self.stamp(md5) == stamp

    # ---- filing and deciding
    @staticmethod
    def _fail(reason):
        return {"ok": False, "reason": reason}

    def request(self, md5, text, target, by, kind="send", operator_only=False):
        if md5_of(text) != md5:
            return self._fail("md5 does not match the text")
        with self._lock():
            if self.state(md5)["state"] in ("pending", "open", "held"):
                return self._fail("already filed and undecided")
            self._append({"event": "request", "md5": md5, "text": text, "target": target, "by": by,
                          "kind": kind, "operator_only": bool(operator_only)})
        return {"ok": True}

    def _operator_message(self, msg_id, source, now):
        m = source.lookup(msg_id)
        if not m:
            return None, "no authenticated record of message %s" % msg_id
        if not m.get("from_operator") or m.get("automated"):
            return None, "message is not a direct operator message"
        if m["ts"] > now + CLOCK_SKEW_S or now - m["ts"] > MESSAGE_MAX_AGE_S:
            return None, "message is stale"
        if str(msg_id) in self.used_messages():
            return None, "message already spent on another decision"
        w = words_of(m["body"])
        if not (w & YES) or (w & NO):
            return None, "message is not a clear go"
        return m, None

    def grant_by_message(self, md5, msg_id, source, by, ttl=GRANT_TTL_S):
        if by not in self.approvers:
            return self._fail("only an approver identity may grant")
        with self._lock():
            now = self.clock()
            s = self.state(md5, now)
            if s["state"] != "pending":
                return self._fail("cannot grant from state %s; a re-send needs a fresh request" % s["state"])
            m, why = self._operator_message(msg_id, source, now)
            if why:
                return self._fail(why)
            if m["ts"] < s["request"]["ts"]:
                return self._fail("message predates the request")
            pending = {e["md5"] for e in self.events() if e["event"] == "request"
                       and self.state(e["md5"], now)["state"] == "pending"}
            named = {p for p in pending if any(p.startswith(t) for t in hex_tokens(m["body"]))}
            if named != {md5}:
                return self._fail("message must name this md5 and no other pending one")
            self._append({"event": "grant", "md5": md5, "by": by, "auth": "operator", "msg": str(msg_id),
                          "expires": now + ttl})
        return {"ok": True}

    def delegate(self, name, classes, msg_id, source, by, ttl=30 * 86400):
        if by not in self.approvers:
            return self._fail("only an approver identity may delegate")
        with self._lock():
            now = self.clock()
            m, why = self._operator_message(msg_id, source, now)
            if why:
                return self._fail(why)
            if name not in str(m["body"]).lower():
                return self._fail("message does not name the delegation")
            self._append({"event": "delegate", "name": name, "classes": sorted(classes), "msg": str(msg_id),
                          "by": by, "expires": now + ttl})
        return {"ok": True}

    def revoke_delegation(self, name, by, reason):
        if by not in self.approvers:
            return self._fail("only an approver identity may revoke")
        self._append({"event": "delegate-revoke", "name": name, "by": by, "reason": reason})
        return {"ok": True}

    def grant_under(self, md5, name, by, ttl=2 * 3600):
        if by not in self.approvers:
            return self._fail("only an approver identity may grant")
        with self._lock():
            now = self.clock()
            s, d = self.state(md5, now), self.delegation(name, now)
            if s["state"] != "pending":
                return self._fail("a delegation cannot grant from state %s" % s["state"])
            if d["state"] != "open":
                return self._fail("delegation is %s" % d["state"])
            if s["request"].get("operator_only") or s["request"]["kind"] not in d["ev"]["classes"]:
                return self._fail("request is outside the delegated classes")
            self._append({"event": "grant", "md5": md5, "by": by, "auth": "delegation", "under": name,
                          "expires": min(now + ttl, d["ev"]["expires"])})
        return {"ok": True}

    # ---- using and closing
    def check(self, md5, caller=None):
        """Read-only. Returns (exit_code, text). Never consumes."""
        now = self.clock()
        s = self.state(md5, now)
        last = s["last"] or {}
        if s["state"] == "consumed" and last.get("via") == "gate" and last.get("by") == caller \
                and 0 <= now - last["ts"] <= CHECK_GRACE_S:
            # The gate consumed for the command now running; this check is part of that command.
            return 0, "open (in flight: consumed %.1fs ago for this caller's running command)" % (now - last["ts"])
        return CODES.get(s["state"], 3), s["state"] + (" (%s)" % s["why"] if s.get("why") else "")

    def consume(self, md5, text, by, what, via="gate"):
        with self._lock():
            now = self.clock()
            try:
                events = self.events(strict=True)
            except LedgerCorrupt as e:
                return self._fail("ledger unreadable, refusing: %s" % e)
            s = self.state(md5, now, events)
            if s["state"] != "open":
                return {"ok": False, "reason": "not open: " + s["state"], "code": CODES.get(s["state"], 3)}
            if md5_of(text) != md5:
                return self._fail("the text being sent is not the text that was granted")
            self._append({"event": "consume", "md5": md5, "by": by, "via": via, "what": str(what)[:300]})
        return {"ok": True}

    def close_done(self, md5, by, evidence):
        """For actions the gate cannot see (typing, form fills). Terminal, never a re-open."""
        if not evidence:
            return self._fail("evidence is required")
        with self._lock():
            if self.state(md5)["state"] != "open":
                return self._fail("only an open grant can be closed as done")
            self._append({"event": "done", "md5": md5, "by": by, "evidence": evidence})
        return {"ok": True}

    def revoke(self, md5, by, reason, outcome=None):
        if by not in self.approvers:
            return self._fail("only an approver identity may revoke")
        if self.state(md5)["state"] == "unknown":
            return self._fail("unknown md5; check for a typo")
        self._append({"event": "revoke", "md5": md5, "by": by, "reason": reason, "outcome": outcome})
        return {"ok": True}

    def deny(self, md5, by, reason):
        if by not in self.approvers or self.state(md5)["state"] != "pending":
            return self._fail("only an approver may deny a pending request")
        self._append({"event": "deny", "md5": md5, "by": by, "reason": reason})
        return {"ok": True}

    # ---- housekeeping
    def sweep(self):
        """Write explicit expiry rows so no reader has to infer a lapse."""
        now, wrote = self.clock(), []
        for md5 in sorted({e["md5"] for e in self.events() if e.get("md5")}):
            s = self.state(md5, now)
            if s["state"] == "pending" and now - s["request"]["ts"] > REQUEST_TTL_S:
                wrote.append(self._append({"event": "expire", "md5": md5, "outcome": "undecided"}))
            elif s["state"] == "expired" and s["last"]["event"] == "grant":
                wrote.append(self._append({"event": "expire", "md5": md5, "outcome": "grant-lapsed"}))
        return wrote

    def open_older_than(self, age_s):
        """Open grants that stayed open past age_s: a sent-but-unclosed action looks exactly like this."""
        now = self.clock()
        out = []
        for md5 in sorted({e["md5"] for e in self.events() if e.get("md5")}):
            s = self.state(md5, now)
            if s["state"] == "open" and now - s["last"]["ts"] > age_s:
                out.append(md5)
        return out
