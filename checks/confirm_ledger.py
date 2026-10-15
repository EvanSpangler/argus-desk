"""Positive-confirmation ledger (stdlib only).

An irreversible outward action may run only against an open grant. A grant exists only because the
operator, in a message the harness can authenticate, named the md5 of the exact text and said go.
The action consumes the grant once. Silence, old messages, and grants for other text never count.
Time is epoch seconds, injected through `clock` so tests are deterministic.

What the desk uses: request, grant_by_message, consume, deny, state, sweep. The delegation and
close-without-gate paths of the original reference implementation are not part of the desk and were
removed in v0.6 (git history has them).
"""
import fcntl, hashlib, json, os, re, time
from contextlib import contextmanager

REQUEST_TTL_S = 24 * 3600      # a request nobody decided is swept
GRANT_TTL_S = 6 * 3600         # default life of a grant
MESSAGE_MAX_AGE_S = 15 * 60    # a message older than this cannot back a new grant
CLOCK_SKEW_S = 60
YES = {"go", "approve", "approved", "yes", "send", "ok", "confirmed"}
NO = {"no", "not", "hold", "wait", "stop", "deny", "denied", "dont", "don't", "cancel", "revoke"}
CODES = {"open": 0, "unknown": 3, "expired": 4, "consumed": 5, "denied": 7, "withdrawn": 8, "pending": 9}

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
    def state(self, md5, now=None, events=None):
        """The state of one md5. Pass `events` (one read of the file) when calling this in a loop."""
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
        simple = {"request": "pending", "consume": "consumed", "deny": "denied", "withdraw": "withdrawn", "expire": "expired"}
        out = {"state": simple.get(last["event"], "unknown"), "grant": grant, "request": req, "last": last}
        if last["event"] == "grant":
            out["state"] = "expired" if last["expires"] < now else "open"
        return out

    def used_messages(self, events=None):
        return {str(e["msg"]) for e in (self.events() if events is None else events) if e["event"] == "grant" and e.get("msg")}

    # ---- filing and deciding
    @staticmethod
    def _fail(reason):
        return {"ok": False, "reason": reason}

    def request(self, md5, text, target, by, kind="send"):
        if md5_of(text) != md5:
            return self._fail("md5 does not match the text")
        with self._lock():
            if self.state(md5)["state"] in ("pending", "open"):
                return self._fail("already filed and undecided")
            self._append({"event": "request", "md5": md5, "text": text, "target": target, "by": by, "kind": kind})
        return {"ok": True}

    def _operator_message(self, msg_id, source, now, events):
        m = source.lookup(msg_id)
        if not m:
            return None, "no authenticated record of message %s" % msg_id
        if not m.get("from_operator") or m.get("automated"):
            return None, "message is not a direct operator message"
        if m["ts"] > now + CLOCK_SKEW_S or now - m["ts"] > MESSAGE_MAX_AGE_S:
            return None, "message is stale"
        if str(msg_id) in self.used_messages(events):
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
            events = self.events()
            s = self.state(md5, now, events)
            if s["state"] != "pending":
                return self._fail("cannot grant from state %s; a re-send needs a fresh request" % s["state"])
            m, why = self._operator_message(msg_id, source, now, events)
            if why:
                return self._fail(why)
            if m["ts"] < s["request"]["ts"]:
                return self._fail("message predates the request")
            pending = {e["md5"] for e in events if e["event"] == "request" and self.state(e["md5"], now, events)["state"] == "pending"}
            named = {p for p in pending if any(p.startswith(t) for t in hex_tokens(m["body"]))}
            if named != {md5}:
                return self._fail("message must name this md5 and no other pending one")
            self._append({"event": "grant", "md5": md5, "by": by, "auth": "operator", "msg": str(msg_id), "expires": now + ttl})
        return {"ok": True}

    # ---- using and closing
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

    def deny(self, md5, by, reason):
        if by not in self.approvers or self.state(md5)["state"] != "pending":
            return self._fail("only an approver may deny a pending request")
        self._append({"event": "deny", "md5": md5, "by": by, "reason": reason})
        return {"ok": True}

    # ---- housekeeping
    def sweep(self):
        """Write explicit expiry rows so no reader has to infer a lapse: a request undecided past
        REQUEST_TTL_S, and a grant past its expiry. One read of the file; writes only when something lapsed."""
        now, wrote = self.clock(), []
        events = self.events()
        for md5 in sorted({e["md5"] for e in events if e.get("md5")}):
            s = self.state(md5, now, events)
            if s["state"] == "pending" and now - s["request"]["ts"] > REQUEST_TTL_S:
                wrote.append(self._append({"event": "expire", "md5": md5, "outcome": "undecided"}))
            elif s["state"] == "expired" and s["last"]["event"] == "grant":
                wrote.append(self._append({"event": "expire", "md5": md5, "outcome": "grant-lapsed"}))
        return wrote
