#!/usr/bin/env python3
"""argus-desk: the desk's own tools served over MCP (stdio), stdlib only.

Why this exists (2026-10-15, the Argus workstation): the mod registers file_draft, skip_ticket, desk_finding and
desk_receipt with $.tool.register and answers them in tool.call hooks. On the workstation a call from inside a
spawned subagent reached no hook ("no tool.call hook answered this call"), whatever the driver's lineage. An MCP
server attached to the agent type (AgentSpec.mcpServers) is served by this process, not by a hook, so the call
cannot go unanswered. The option agentTools=mcp switches the agent types to it; the hooks stay for the main session.

Each tool runs the same desk.py sequence the hook runs, one subprocess per call, so the record is identical:
  file_draft    cite (when citations) -> file -> mark drafting
  skip_ticket   mark skipped -> finding (summary)
  desk_finding  finding
  desk_receipt  receipt
Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP 2024-11-05): initialize, notifications/initialized,
ping, tools/list, tools/call. Anything else answers -32601. Nothing is written but through desk.py.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, "desk.py")
VERSION = "0.8.8"

TOOLS = [
    {"name": "file_draft",
     "description": "File a customer-facing reply for the operator to review before it is posted. Required before any outward post: "
                    "the posting tool refuses text the operator has not granted. Give the exact text that would be sent, and citations: "
                    "for every factual sentence, the receipt id (from desk_receipt) that carries it. When revising a draft the operator sent back, pass supersedes with the md5 you were given.",
     "inputSchema": {"type": "object", "properties": {
         "ticket": {"type": "string", "description": "The ticket id the reply goes to"},
         "draft": {"type": "string", "description": "The exact reply text that would be posted"},
         "citations": {"type": "array", "items": {"type": "object", "properties": {"sentence": {"type": "string"}, "receipt": {"type": "string"}}, "required": ["sentence", "receipt"]},
                       "description": "Every factual sentence of the draft with the receipt id that carries it"},
         "supersedes": {"type": "string", "description": "md5 of the draft this one replaces, when revising"}},
         "required": ["ticket", "draft"]}},
    {"name": "skip_ticket",
     "description": "Record that a ticket the desk pulled needs no reply from us right now, with the reason. The operator sees it in the desk.",
     "inputSchema": {"type": "object", "properties": {"ticket": {"type": "string"}, "reason": {"type": "string"}}, "required": ["ticket", "reason"]}},
    {"name": "desk_finding",
     "description": "Record a finding on a ticket investigation: kind hypothesis, evidence, counter (evidence against), objection (a reviewer's), "
                    "answer (to an objection) or summary. Name the receipt ids that carry it; about names the finding it answers or objects to.",
     "inputSchema": {"type": "object", "properties": {"ticket": {"type": "string"}, "kind": {"type": "string"}, "text": {"type": "string"},
                                                      "receipts": {"type": "array", "items": {"type": "string"}}, "about": {"type": "string"}},
                     "required": ["ticket", "kind", "text"]}},
    {"name": "desk_receipt",
     "description": "Mint a receipt for something you actually read on this ticket: kind \"docs\" with the sentence verbatim from an official documentation "
                    "page and its URL (the page must have been read in this run), \"material\" with the customer's own words verbatim from the thread, "
                    "or \"test\" with recorded output of a real run and its source. A sentence nobody read is refused.",
     "inputSchema": {"type": "object", "properties": {"ticket": {"type": "string"}, "kind": {"type": "string"}, "quote": {"type": "string"},
                                                      "source": {"type": "string", "description": "the page URL, for docs; the run, for test"}},
                     "required": ["ticket", "kind", "quote"]}},
    {"name": "desk_mismatch",
     "description": "Record the basis an escalation needs: the docs receipt id (what the product should do), the material or test receipt id "
                    "(what it did), and one line on how they differ. Both receipts must be on this ticket and fresh.",
     "inputSchema": {"type": "object", "properties": {"ticket": {"type": "string"}, "documented": {"type": "string"}, "observed": {"type": "string"},
                                                      "summary": {"type": "string"}}, "required": ["ticket", "documented", "observed", "summary"]}},
]


class Desk:
    """One desk.py call per method, the way the mod's desk() runs it."""

    def __init__(self, root, docs_hosts, python=None):
        self.root, self.docs_hosts, self.python = root, docs_hosts, python or sys.executable

    def run(self, args, body=None):
        env = dict(os.environ, ARGUS_DESK_DOCS_HOSTS=self.docs_hosts)
        try:
            r = subprocess.run([self.python, "-I", DESK, "--root", self.root] + list(args), input=json.dumps(body) if body is not None else "",
                               capture_output=True, text=True, timeout=60, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "reason": "desk.py did not run: %s" % e}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"ok": False, "reason": "desk.py exit %s: %s" % (r.returncode, (r.stderr or r.stdout)[:300])}


def s(v):
    return "" if v is None else str(v)


def handle(desk, role, name, a):
    """The tool's text answer and whether it is an error, mirroring the hook's words so an agent reads the same thing."""
    ticket = s(a.get("ticket"))
    if name == "file_draft":
        cited = ""
        cits = a.get("citations")
        if isinstance(cits, list) and cits:
            c = desk.run(["cite"], {"ticket": ticket, "citations": cits})
            unknown = c.get("unknown") or []
            cited = "; citations bound: %s%s" % (c.get("bound", 0), (", unknown receipts: " + ", ".join(map(str, unknown))) if unknown else "")
        r = desk.run(["file"], {"ticket": ticket, "draft": s(a.get("draft")), "supersedes": a.get("supersedes"), "source": "argus", "by": role})
        if not r.get("ok"):
            return "Not filed: %s%s" % (r.get("reason"), (" (md5 %s)" % r["md5"]) if r.get("md5") else ""), True
        desk.run(["mark", "--ticket", s(r.get("ticket")), "--state", "drafting"])
        hits, warn, claims = r.get("hits") or [], r.get("warnings") or [], r.get("claims")
        return ("Filed draft %s for #%s. Checks: %s%s%s%s%s. It is waiting for the operator at the desk. Do not post it until you are told "
                "it was approved; if it comes back with a note, revise and file again with supersedes set to this md5."
                % (r.get("md5"), r.get("ticket"), r.get("verdict"), (" (%s)" % ", ".join(hits)) if hits else "",
                   ("; warnings: " + ", ".join(warn)) if warn else "",
                   ("; claims: %s of %s receipted" % (claims.get("credited"), claims.get("checkable"))) if isinstance(claims, dict) else "", cited)), False
    if name == "skip_ticket":
        reason = s(a.get("reason"))
        desk.run(["mark", "--ticket", ticket, "--state", "skipped", "--note", reason])
        desk.run(["finding"], {"ticket": ticket, "agent": role, "role": "drafter", "kind": "summary", "text": "Skipped: " + reason})
        return "Recorded: #%s needs no reply now. The desk will pull the next open ticket." % ticket, False
    if name == "desk_finding":
        r = desk.run(["finding"], {"ticket": ticket, "agent": role, "role": role, "kind": s(a.get("kind")), "text": s(a.get("text")),
                                   "receipts": a.get("receipts"), "about": a.get("about")})
        if r.get("ok"):
            return "Recorded %s (%s)." % (r.get("id"), a.get("kind")), False
        return "Not recorded: %s" % r.get("reason"), True
    if name == "desk_receipt":
        r = desk.run(["receipt"], {"ticket": ticket, "kind": s(a.get("kind")), "quote": s(a.get("quote")), "source": a.get("source"), "by": role})
        if r.get("ok"):
            return "Receipt %s." % r.get("id"), False
        why = r.get("why") or r.get("reason")
        # 2026-10-15 (receipt fault): the refusal says what the desk holds, so a byte-exact quote refused is readable as what it is
        more = ""
        if why == "page-not-captured":
            cap = r.get("captured") or []
            mine = r.get("captured_this_run") or []
            more = ("; fetch the page in this run first, then quote it with the URL as the result names it (this run captured %s; newest on any ticket: %s)"
                    % (", ".join(mine) if mine else "NO page: no docs read in this run named an official URL", ", ".join(cap) if cap else "none"))
        elif why == "no-thread-captured":
            more = "; " + s(r.get("hint"))
        elif why == "quote-not-in-material":
            t = r.get("thread") or {}
            more = "; the captured thread holds %s customer message(s), %s chars; quote the customer's words as the proxy returned them" % (t.get("customer_messages"), t.get("customer_chars"))
        elif why == "quote-not-from-customer":
            more = "; those words are in a %s message, not the customer's" % r.get("found_in")
        return "Refused: %s. Only a sentence you actually read, verbatim, can be receipted%s." % (why, more), True
    if name == "desk_mismatch":
        r = desk.run(["mismatch"], {"ticket": ticket, "documented": s(a.get("documented")), "observed": s(a.get("observed")),
                                    "summary": s(a.get("summary")), "by": role})
        if r.get("ok"):
            return "Mismatch %s." % r.get("id"), False
        return "Refused: %s." % (r.get("why") or r.get("reason")), True
    return "unknown tool %s" % name, True


def serve(desk, role, inp=sys.stdin, out=sys.stdout):
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if mid is None:          # a notification: nothing to answer
            continue
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "argus-desk", "version": VERSION}}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            # 2026-10-15 (receipt fault): a handler that raised killed the server, and every later call from that agent answered
            # "Connection closed". The error is the tool's answer now; the server keeps serving.
            try:
                text, err = handle(desk, role, s(params.get("name")), params.get("arguments") or {})
            except Exception as e:  # noqa: BLE001
                text, err = "desk tool %s failed: %s: %s" % (s(params.get("name")), type(e).__name__, str(e)[:300]), True
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}) + "\n")
            out.flush()
            continue
        # ASCII on the wire: a stdout whose encoding is not UTF-8 cannot raise on a quote with an accent or a dash
        out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}, ensure_ascii=True) + "\n")
        out.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description="argus-desk tools over MCP stdio")
    ap.add_argument("--root", default="~/.argus-desk")
    ap.add_argument("--role", default="model", help="the agent type this server is attached to; recorded as the author")
    ap.add_argument("--docs-hosts", default=os.environ.get("ARGUS_DESK_DOCS_HOSTS", "docs.wiz.io"))
    a = ap.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(errors="replace")   # a byte the locale cannot decode must not end the server
        except (AttributeError, ValueError):
            pass
    serve(Desk(os.path.expanduser(a.root), a.docs_hosts), a.role)


if __name__ == "__main__":
    main()
