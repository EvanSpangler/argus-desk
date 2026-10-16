import os, sys, json, tempfile, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import desk as D

T0 = 1_800_000_000.0

class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t

class Sam(dict):
    """Every ticket's requester is Sam Lee, so the fixtures' "Hi Sam," passes the requester check; RequesterTests use the registry."""
    def get(self, k, default=None): return {"name": "Sam Lee"}

def mk(requesters=True):
    root = tempfile.mkdtemp()
    c = Clock()
    d = D.Desk(root, clock=c)
    if requesters:
        d._requesters = Sam
    return d, c

DRAFT = "Hi Sam,\n\nThe broker for this connector is disabled, so the connector reports unhealthy. Re-enable the broker in the portal under Settings > Connectors, then run a fresh scan.\n\nPlease send a fresh support package after that.\n\nThanks!\n"

class DeskTests(unittest.TestCase):
    def test_file_then_list_then_grant_then_consume_once(self):
        d, c = mk()
        r = d.file("123456", DRAFT)
        self.assertTrue(r["ok"], r); md5 = r["md5"]
        self.assertEqual(d.list()["items"][0]["state"], "pending")
        self.assertEqual(d.consume([DRAFT], "post")["ok"], False, "no grant yet")
        g = d.grant(md5, "press-1"); self.assertTrue(g["ok"], g)
        self.assertEqual(d.list()["items"][0]["state"], "open")
        self.assertTrue(d.consume([DRAFT], "post", "pending")["ok"])
        self.assertFalse(d.consume([DRAFT], "post again", "pending")["ok"], "one grant, one send")
        self.assertEqual(d.list()["items"], [])

    def test_edited_text_does_not_ride_on_the_grant(self):
        d, c = mk()
        md5 = d.file("1", DRAFT)["md5"]; d.grant(md5, "p")
        self.assertFalse(d.consume([DRAFT.replace("fresh scan", "new scan")], "post")["ok"])

    def test_deny_and_modify_record_decisions_and_edit_pair(self):
        d, c = mk()
        a = d.file("2", DRAFT)["md5"]
        self.assertTrue(d.deny(a, "wrong ask")["ok"])
        b = d.file("2", DRAFT + "PS: thanks.\n")["md5"]
        c.t += 120
        m = d.modify(b, "drop the PS"); self.assertTrue(m["ok"], m)
        r = d.file("2", DRAFT.replace("Thanks!", "Thank you!"), supersedes=b)
        self.assertTrue(r["ok"]); c.t += 30
        d.grant(r["md5"], "p2")
        s = d.stats()
        self.assertEqual(s["decisions"], {"send": 1, "deny": 1, "modify": 1})
        self.assertEqual(s["sent_after_edit"], 1)
        self.assertEqual(s["edit_distance"]["n"], 1)
        self.assertEqual(s["wait_s"]["median"], 30)

    def test_a_redraft_takes_the_place_of_the_draft_it_supersedes(self):
        d, c = mk()
        a = d.file("4", DRAFT)["md5"]
        b = d.file("4", DRAFT.replace("Thanks!", "Thank you!"), supersedes=a)["md5"]
        self.assertEqual([i["md5"] for i in d.list()["items"]], [b], "only the newest draft waits")
        self.assertEqual(d.ledger.state(a)["state"], "withdrawn")
        g = d.grant(a, "p-old")
        self.assertFalse(g["ok"]); self.assertIn("superseded by", g["reason"])
        self.assertTrue(d.grant(b, "p-new")["ok"])

    def test_a_rerun_draft_replaces_the_earlier_runs_draft_on_the_ticket(self):
        d, c = mk()
        a = d.file("6", DRAFT)["md5"]
        other = d.file("7", DRAFT + "PS.\n")["md5"]
        b = d.file("6", DRAFT.replace("Thanks!", "Thank you!"))["md5"]
        self.assertEqual(sorted(i["md5"] for i in d.list()["items"]), sorted([other, b]))
        self.assertEqual(d.ledger.state(a)["state"], "withdrawn")
        self.assertEqual(d.ledger.state(other)["state"], "pending", "another ticket's draft is left alone")
        self.assertFalse(d.grant(a, "p")["ok"])

    def test_superseded_drafts_filed_before_the_withdraw_stay_off_the_desk(self):
        # records written before 0.8.1 left the superseded draft pending in the ledger
        d, c = mk()
        a = d.file("5", DRAFT)["md5"]
        b = D.md5_of(D._norm_draft(DRAFT + "x\n"))
        d.ledger.request(b, DRAFT + "x\n", "Zendesk #5 public reply", "argus", kind="send")
        d._append(d.requests_path, {"md5": b, "ticket": "5", "source": "argus", "supersedes": a, "chars": 1,
                                    "checks": {"verdict": "clear", "hits": [], "warnings": []}, "text": DRAFT + "x\n"})
        self.assertEqual(d.ledger.state(a)["state"], "pending")
        self.assertEqual([i["md5"] for i in d.list()["items"]], [b])
        self.assertFalse(d.grant(a, "p")["ok"])
        # the newest denied, the older one is not offered in its place
        d.deny(b, "rerun it")
        self.assertEqual([i["md5"] for i in d.list()["items"]], [])

    def test_press_id_is_single_spend(self):
        d, c = mk()
        a = d.file("3", DRAFT)["md5"]; b = d.file("33", DRAFT + "x\n")["md5"]
        self.assertTrue(d.grant(a, "same-press")["ok"])
        self.assertFalse(d.grant(b, "same-press")["ok"])

    def test_lint_holds_dash_and_timeline(self):
        d, c = mk()
        r = d.file("4", "Hi,\n\nI will get back to you shortly — the team is looking.\n")
        self.assertEqual(r["verdict"], "hold"); self.assertIn("dash", r["hits"]); self.assertIn("timeline-commitment", r["hits"])

    def test_cli_call_reads_its_body_from_stdin(self):
        # 2026-10-16: `call` was not among the stdin commands, so every call record was lost ("0 call(s)" on #147036)
        import subprocess
        root = tempfile.mkdtemp()
        body = json.dumps({"ticket": "147036", "agent": "inv1", "tool": "mcp__x__invoke_proxy", "args": {"endpoint": "zendesk_get_ticket_comments"}, "result": "abc"})
        r = subprocess.run([sys.executable, "-I", D.__file__, "--root", root, "call"], input=body, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(D.Desk(root).calls("147036")["calls"]), 1)

    def test_calls_shows_each_read_and_what_it_gave(self):
        # 2026-10-16: #147036 ran with the thread never recorded and nobody could say from the workstation what the reads returned
        d, c = mk()
        d.call("147036", "inv1", "mcp__local-argus__invoke_proxy", {"proxy": "zendesk-full", "endpoint": "zendesk_list_comments"}, "HTTP 404", ok=False)
        d.call("147036", "inv1", "mcp__local-argus__invoke_proxy", {"proxy": "zendesk-full", "endpoint": "zendesk_get_ticket_comments"}, "x" * 50)
        d.event("147036", "inv1", "capture", "zendesk_get_ticket_comments: no comments found in a 50-char result: xxx")
        out = d.calls("147036")
        self.assertTrue(out["ok"]); self.assertEqual(len(out["calls"]), 2); self.assertFalse(out["thread"]["recorded"])
        text = D.calls_text(out)
        self.assertIn("thread not recorded", text); self.assertIn("zendesk_list_comments FAILED", text)
        self.assertIn("no comments found in a 50-char result", text)
        d.capture("147036", [{"id": "1", "who": "customer", "at": 1, "text": "Which CPE?"}], 1)
        self.assertIn("thread 1 message(s)", D.calls_text(d.calls("147036")))
        self.assertIn("captured: nothing", D.calls_text(d.calls("147037")))

    def test_thread_capture_drives_cadence_and_questions(self):
        d, c = mk()
        d.capture("5", [{"id": "1", "who": "customer", "at": T0 - 7200, "text": "Scans fail. Which agent version is required?"},
                        {"id": "2", "who": "us", "at": T0 - 3600, "text": "Please send the logs."}], count=2)
        r = d.file("5", DRAFT)
        self.assertEqual(r["thread"]["status"], "captured")
        self.assertIn("too-soon", r["hits"])
        self.assertIn("repeat-ask", r["hits"])
        self.assertEqual(r["thread"]["open_questions"], [])  # question predates our last send
        tr = d.trace("5")
        self.assertEqual([m["who"] for m in tr["thread"]["messages"]], ["customer", "us"])  # the pane shows the thread it checked against
        self.assertEqual(tr["thread_count"], 2)
        self.assertIsNone(d.trace("404")["thread"])

    def test_unrecorded_comment_holds(self):
        d, c = mk()
        d.capture("6", [{"id": "1", "who": "customer", "at": T0 - 90000, "text": "Hello?"}], count=3)
        r = d.file("6", DRAFT)
        self.assertIn("unrecorded-comment", r["hits"])

    def test_claims_hold_live(self):
        d, c = mk()
        r = d.file("9", DRAFT)
        self.assertGreater(r["claims"]["checkable"], 0)
        self.assertEqual(r["claims"]["credited"], 0)
        self.assertEqual(r["verdict"], "hold", r)
        self.assertIn("uncovered", r["hits"], r)
        self.assertFalse(r["claims"]["shadow"])

    def test_voice_rules_hold_on_block_and_warn_on_warn(self):
        d, c = mk()
        r = d.file("9", "Hi,\n\nYou're absolutely right. Let me know if that helps.")
        self.assertIn("voice:absolutely-right", r["hits"], r)
        self.assertIn("voice:let-me-know-if", r["warnings"], r)
        with open(os.path.join(d.root, "voice-rules.json"), "w") as fh:
            json.dump({"rules": [{"id": "custom-word", "pattern": "synergy", "severity": "block", "channels": ["all"]}]}, fh)
        r = d.file("9", "Hi,\n\nWe will look for synergy here.")
        self.assertIn("voice:custom-word", r["hits"], r)
        self.assertNotIn("voice:absolutely-right", r["hits"])
    def test_cli_roundtrip(self):
        import subprocess
        root = tempfile.mkdtemp(); here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desk.py")
        run = lambda args, inp=None: json.loads(subprocess.run([sys.executable, "-I", here, "--root", root] + args, input=inp, capture_output=True, text=True).stdout)
        r = run(["file"], json.dumps({"ticket": "8", "draft": DRAFT}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(run(["list"])["items"]), 1)
        self.assertTrue(run(["grant", "--md5", r["md5"], "--press-id", "p"])["ok"])
        self.assertTrue(run(["consume"], json.dumps({"candidates": [DRAFT], "what": "POST comments", "status": "pending"}))["ok"])
        self.assertEqual(run(["stats"])["decisions"]["send"], 1)

    def test_grant_status_must_be_on_the_posting_call(self):
        d, c = mk()
        md5 = d.file("123456", DRAFT)["md5"]
        self.assertTrue(d.grant(md5, "press-1", status="solved")["ok"])
        r = d.consume([DRAFT], "post", None)
        self.assertEqual(r.get("code"), "status-missing"); self.assertEqual(r["status"], "solved")
        self.assertEqual(d.list()["items"][0]["state"], "open", "the grant is left open")
        r = d.consume([DRAFT], "post", "pending")
        self.assertEqual(r.get("code"), "status-missing", "the wrong status is not the status")
        r = d.consume([DRAFT], "post", "solved")
        self.assertTrue(r["ok"]); self.assertEqual(r["status"], "solved"); self.assertEqual(r["ticket"], "123456")
        self.assertEqual(d._queue()["123456"]["state"], "sent")
        self.assertFalse(d.grant(md5, "press-2", status="closed")["ok"], "unknown status refused")

    def test_sync_orders_the_pull_and_confirms_the_transition(self):
        d, c = mk()
        v = d.sync([{"id": 11, "status": "open", "updated": "2026-10-15T01:00:00Z"},
                    {"id": 12, "status": "new", "sla": "2026-10-15T09:00:00Z", "updated": "2026-10-15T02:00:00Z"},
                    {"id": 13, "status": "open", "updated": "2026-10-15T03:00:00Z"}])
        self.assertEqual(v["next"]["ticket"], "12", "the SLA chip first")
        d._mark("12", "drafting")
        self.assertIsNone(d.queue_view()["next"], "one ticket at a time")
        md5 = d.file("12", DRAFT)["md5"]
        self.assertEqual(d.queue_view()["rows"][0]["state"], "waiting")
        d.grant(md5, "p1"); d.consume([DRAFT], "post", "pending")
        self.assertEqual(d._queue()["12"]["state"], "sent")
        v = d.queue_view(); self.assertEqual(v["next"]["ticket"], "11", "oldest next")
        # the next poll: 12 is pending now and 13 was taken by someone else
        v = d.sync([{"id": 11, "status": "open", "updated": "2026-10-15T01:00:00Z"}])
        q = d._queue()
        self.assertEqual(q["12"]["state"], "done"); self.assertEqual(q["12"]["transition"], "confirmed")
        self.assertEqual(q["13"]["state"], "gone")
        self.assertEqual(d.stats()["transitions"], {"posted": 1, "confirmed": 1})
        # the customer answers: 12 is open again with a newer update
        v = d.sync([{"id": 12, "status": "open", "updated": "2026-10-15T05:00:00Z"}])
        self.assertEqual(d._queue()["12"]["state"], "reopened")
        self.assertEqual(v["next"]["ticket"], "12", "a reopened ticket outranks a new one")
        # a pull nobody filed on goes stale and is offered again
        d._mark("11", "drafting"); c.t += D.STALE_S + 1
        self.assertIn(d.queue_view()["next"]["ticket"], ("11", "12"))
        self.assertEqual([r["state"] for r in d.queue_view()["rows"] if r["ticket"] == "11"], ["stale"])


    def test_run_findings_receipts_cite_trace(self):
        d, c = mk()
        D.CR.POLICY["docs_hosts"].append("docs.example.org")
        d.run_update("12", phase="investigate", agent={"id": "a1", "role": "investigator", "model": "sonnet", "status": "running"})
        d.run_update("12", agent={"id": "a1", "status": "completed", "summary": "broker disabled"})
        self.assertEqual(d._run("12")["agents"][0]["status"], "completed")
        self.assertIn("ended", d._run("12")["agents"][0])
        d.event("12", "a1", "tool", "docs_search broker")
        self.assertTrue(d.finding("12", "a1", "investigator", "hypothesis", "The broker is disabled")["ok"])
        self.assertFalse(d.finding("12", "a1", "investigator", "guess", "x")["ok"])
        # a docs receipt needs the page captured and the quote verbatim in its prose
        self.assertEqual(d.receipt("12", "docs", "Re-enable the broker under Settings", "https://docs.example.org/broker")["why"], "page-not-captured")
        d.page("https://docs.example.org/broker", "Broker health\n\nRe-enable the broker under Settings > Connectors to restore scanning.\n")
        r = d.receipt("12", "docs", "Re-enable the broker under Settings", "https://docs.example.org/broker", by="a1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(d.receipt("12", "docs", "words not on the page at all", "https://docs.example.org/broker")["why"], "quote-not-in-prose")
        # material must be in the captured thread
        d.capture("12", [{"id": "1", "who": "customer", "at": T0, "text": "Our connector reports unhealthy since Monday."}], 1)
        self.assertTrue(d.receipt("12", "material", "connector reports unhealthy", by="a1")["ok"])
        self.assertFalse(d.receipt("12", "material", "something they never said")["ok"])
        # cite binds a receipt to a draft sentence and the claim check credits it
        md5 = d.file("12", DRAFT)["md5"]
        sent = "Re-enable the broker in the portal under Settings > Connectors, then run a fresh scan."
        out = d.cite("12", [{"receipt": r["id"], "sentence": sent}, {"receipt": "rcpt:nope", "sentence": sent}])
        self.assertEqual((out["bound"], out["unknown"]), (1, ["rcpt:nope"]))
        t = d.trace("12")
        self.assertEqual(t["run"]["phase"], "investigate")
        self.assertEqual(len(t["findings"]), 1)
        self.assertEqual(len(t["receipts"]), 2, "one row per receipt id after rebinding")
        self.assertEqual(len(t["drafts"]), 1)
        self.assertTrue(any(cl.get("receipts") for cl in t["claims"]), t["claims"])

class AuditTests(unittest.TestCase):
    def test_chain_verifies_and_breaks_on_edit(self):
        d, c = mk()
        d.run_update("7", phase="new", agent={"id": "a1", "role": "investigator", "label": "inv1", "model": "sonnet", "status": "running", "prompt": "read the thread"})
        d.event("7", "inv1", "tool", "invoke_proxy")
        d.call("7", "inv1", "invoke_proxy", {"proxy": "zendesk-full", "endpoint": "zendesk_list_comments"}, result='{"comments":[]}')
        d.run_update("7", agent={"id": "a1", "status": "completed", "answer": "nothing found"})
        v = d.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["files"]["runs/7/calls.jsonl"]["rows"], 1)
        run = d._run("7")
        a = run["agents"][0]
        self.assertEqual(a["prompt_sha"], D.sha_of("read the thread"))
        self.assertTrue(os.path.exists(os.path.join(d.root, a["answer_file"])))
        self.assertNotIn("prompt", a)
        p = os.path.join(d.root, "runs", "7", "events.jsonl")
        with open(p) as fh:
            rows = fh.read().splitlines()
        rows[0] = rows[0].replace("invoke_proxy", "something_else")
        with open(p, "w") as fh:
            fh.write("\n".join(rows) + "\n")
        v = d.verify()
        self.assertFalse(v["ok"])
        self.assertIn("hash mismatch", v["files"]["runs/7/events.jsonl"]["problem"])

    def test_audit_walks_the_chain_and_names_overrules(self):
        d, c = mk()
        d.run_update("8", phase="draft", agent={"id": "a2", "role": "drafter", "label": "draft", "model": "opus", "status": "completed", "prompt": "write it"})
        r = d.file("8", DRAFT)
        self.assertEqual(r["verdict"], "hold")
        g = d.grant(r["md5"], "press-9")
        self.assertTrue(g["ok"], g)
        self.assertIn("uncovered", g["overruled"])
        a = d.audit(md5=r["md5"])
        self.assertTrue(a["ok"], a)
        self.assertEqual(a["decision"]["decision"], "send")
        self.assertIn("uncovered", a["decision"]["overruled"])
        self.assertEqual(a["transitions"][0]["overruled"], a["decision"]["overruled"])
        self.assertTrue(any(s["status"] != "ok" for s in a["sentences"]))
        self.assertEqual(a["agents"][0]["prompt"]["sha"], D.sha_of("write it"))
        self.assertTrue(a["chain"]["ok"])
        txt = D.render_audit(a)
        self.assertIn("OVERRULED uncovered", txt)
        self.assertIn("RECORD INTEGRITY: every chained file verifies", txt)
        self.assertTrue(any(e["kind"] == "overrule" for e in d._rows(os.path.join(d.root, "runs", "8", "events.jsonl"))))
        self.assertEqual(d.audit(ticket="8")["md5"], r["md5"])


class RemediationTests(unittest.TestCase):
    """v0.6: the bugs the review found, each pinned."""

    def test_sent_ticket_still_in_the_open_set_stays_sent(self):
        # Zendesk's search index lags: the ticket we just posted to shows up unchanged on the next poll
        d, c = mk()
        d.sync([{"id": 12, "status": "open", "updated": "2026-10-15T02:00:00Z"}])
        md5 = d.file("12", DRAFT)["md5"]; d.grant(md5, "p1")
        self.assertTrue(d.consume([DRAFT], "post", "pending")["ok"])
        self.assertEqual(d._queue()["12"]["status_set"], "pending")
        c.t += 300
        v = d.sync([{"id": 12, "status": "open", "updated": "2026-10-15T02:00:00Z"}])
        self.assertEqual(d._queue()["12"]["state"], "sent", "unchanged updated_at is not a customer answer")
        self.assertIsNone(v["next"])
        # our own post moved updated_at by a few seconds: still ours
        v = d.sync([{"id": 12, "status": "open", "updated": datetime.fromtimestamp(T0 + 5, timezone.utc).isoformat()}])
        self.assertEqual(d._queue()["12"]["state"], "sent")
        # the customer answers well after the Send
        v = d.sync([{"id": 12, "status": "open", "updated": datetime.fromtimestamp(T0 + 3600, timezone.utc).isoformat()}])
        self.assertEqual(d._queue()["12"]["state"], "reopened")
        self.assertEqual(v["next"]["ticket"], "12")

    def test_readback_updated_at_is_the_line_for_reopened(self):
        d, c = mk()
        d.sync([{"id": 13, "status": "open", "updated": "2026-10-15T02:00:00Z"}])
        md5 = d.file("13", DRAFT)["md5"]; d.grant(md5, "p1"); d.consume([DRAFT], "post", "pending")
        d._mark("13", "sent", sent_updated="2026-10-15T02:00:30Z")     # what the readback after the post saw
        d.sync([{"id": 13, "status": "open", "updated": "2026-10-15T02:00:30Z"}])
        self.assertEqual(d._queue()["13"]["state"], "sent")
        d.sync([{"id": 13, "status": "open", "updated": "2026-10-15T02:00:31Z"}])
        self.assertEqual(d._queue()["13"]["state"], "reopened")
        # a done ticket (it left the open set) that comes back is reopened whatever its timestamps
        d._mark("13", "sent"); d.sync([]); self.assertEqual(d._queue()["13"]["state"], "done")
        self.assertEqual(d._rows(d.transitions_path)[-1]["status"], "pending", "the confirmed row carries the status set")
        d.sync([{"id": 13, "status": "open"}])
        self.assertEqual(d._queue()["13"]["state"], "reopened")

    def test_running_a_ticket_again_starts_its_own_rounds(self):
        d, c = mk()
        d.run_update("5", phase="new", agent={"id": "a1", "role": "investigator", "label": "inv1", "model": "sonnet", "status": "completed"})
        d.run_update("5", phase="review"); d.run_update("5", phase="review")
        self.assertEqual(d._run("5")["rounds"], 2)
        d.run_update("5", phase="new")
        run = d._run("5")
        self.assertEqual((run["rounds"], run["agents"], run["phase"]), (0, [], "new"))
        self.assertEqual(len(run["history"]), 1)
        self.assertEqual(run["history"][0]["agents"][0]["label"], "inv1")

    def test_gate_refuses_a_second_body_and_a_status_elsewhere(self):
        d, c = mk()
        md5 = d.file("7", DRAFT)["md5"]; d.grant(md5, "p1")
        r = d.consume(["Something the operator never saw.", DRAFT], "post", "pending")
        self.assertEqual(r.get("code"), "body-mismatch")
        self.assertEqual(d.list()["items"][0]["state"], "open", "nothing spent")
        r = d.consume([DRAFT, DRAFT.strip()], "post", "pending")
        self.assertTrue(r["ok"], "the same text twice (body and plain body) is one body")

    def test_undecided_draft_is_written_off_after_the_ttl(self):
        d, c = mk()
        d.file("1", DRAFT)
        self.assertEqual(len(d.list()["items"]), 1)
        c.t += D.CL.REQUEST_TTL_S + 1
        self.assertEqual(d.list()["items"], [])
        self.assertEqual(d.ledger.events()[-1]["event"], "expire")
        md5 = d.file("1", DRAFT)["md5"]
        self.assertTrue(d.grant(md5, "p")["ok"]); c.t += D.CL.GRANT_TTL_S + 1
        self.assertEqual(d.list()["items"], [])
        self.assertFalse(d.consume([DRAFT], "post", "pending")["ok"], "a lapsed grant does not post")

    def test_docs_receipt_names_where_the_captured_body_came_from(self):
        d, c = mk()
        D.CR.POLICY["docs_hosts"].append("docs.example.org")
        body = "Result 1: The connector requires outbound access on port 443. Source: https://docs.example.org/a"
        d.page("https://docs.example.org/a", body, origin="search")
        r = d.receipt("3", "docs", "The connector requires outbound access on port 443", "https://docs.example.org/a", by="inv1")
        self.assertTrue(r["ok"], r)
        self.assertEqual([x for x in D.CR.load(d.receipts_path) if x["id"] == r["id"]][0]["page_origin"], "search")
        sent = "The connector requires outbound access on port 443."
        d.file("3", "Hi,\n\n" + sent + "\n")
        d.cite("3", [{"receipt": r["id"], "sentence": sent}])
        a = d.audit(ticket="3")
        chain = [s_ for s_ in a["sentences"] if s_["receipts"]][0]["receipts"][0]
        self.assertEqual(chain["captured"]["origin"], "search")
        self.assertIn("docs search result naming this URL", D.render_audit(a))
        d.page("https://docs.example.org/b", "Broker health\n\nRe-enable the broker to restore scanning.\n")
        r2 = d.receipt("3", "docs", "Re-enable the broker to restore scanning", "https://docs.example.org/b")
        self.assertEqual([x for x in D.CR.load(d.receipts_path) if x["id"] == r2["id"]][0]["page_origin"], "fetch")

    def test_receipt_fault_material_refusals_say_what_the_desk_holds(self):
        # 2026-10-15, workstation: verbatim customer quotes came back quote-not-in-material; the desk could not say why.
        d, c = mk()
        r = d.receipt("5", "material", "the sensors are all on the latest version", by="inv1")
        self.assertEqual(r["why"], "no-thread-captured", r)
        self.assertIn("read every comment", r["hint"])
        d.capture("5", [{"id": "1", "who": "customer", "at": T0 - 600, "text": "<div>The sensors are all on the&nbsp;latest version.<br>Please assist.</div>"},
                        {"id": "2", "who": "us", "at": T0 - 300, "text": "Please send the diagnostic bundle."}], 2)
        ok = d.receipt("5", "material", "The sensors are all on the latest version.", by="inv1")
        self.assertTrue(ok["ok"], ok)   # an html body still carries the customer's words
        r = d.receipt("5", "material", "Please send the diagnostic bundle.", by="inv1")
        self.assertEqual(r["why"], "quote-not-from-customer", r); self.assertEqual(r["found_in"], "us")
        r = d.receipt("5", "material", "something nobody wrote", by="inv1")
        self.assertEqual(r["why"], "quote-not-in-material", r)
        self.assertEqual(r["thread"]["customer_messages"], 1); self.assertGreater(r["thread"]["customer_chars"], 10)
        kinds = [e["kind"] for e in d.trace("5")["events"]]
        self.assertEqual(kinds.count("receipt-refused"), 3, kinds); self.assertIn("receipt", kinds)

    def test_receipt_fault_docs_page_found_under_the_url_however_it_is_written(self):
        d, c = mk()
        D.CR.POLICY["docs_hosts"].append("docs.example.org")
        d.page("https://docs.example.org/docs/how-detection-works/", "Detection runs on the latest scan of each workload.", origin="fetch")
        for src in ("docs.example.org/docs/how-detection-works", "https://docs.example.org/docs/how-detection-works#top",
                    "https://docs.example.org/docs/how-detection-works?x=1", "HTTPS://DOCS.example.org/docs/how-detection-works/"):
            r = d.receipt("6", "docs", "Detection runs on the latest scan of each workload", src, by="inv1")
            self.assertTrue(r["ok"], (src, r))
        r = d.receipt("6", "docs", "Detection runs on the latest scan of each workload", "https://docs.example.org/docs/other", by="inv1")
        self.assertEqual(r["why"], "page-not-captured")
        self.assertEqual(r["captured"], ["https://docs.example.org/docs/how-detection-works/"])
        self.assertEqual(D.canon_url("https://Docs.Example.org/a/b/?q=1#f"), "docs.example.org/a/b")
        # a page file written by an older version (exact-string key) still resolves
        import hashlib, json
        with open(os.path.join(d.root, "pages", hashlib.md5(b"https://docs.example.org/old").hexdigest() + ".json"), "w") as fh:
            json.dump({"source": "https://docs.example.org/old", "body": "Old pages are still read by the desk.", "origin": "fetch", "ts": 1}, fh)
        self.assertTrue(d.receipt("6", "docs", "Old pages are still read by the desk", "https://docs.example.org/old")["ok"])

    def test_torn_run_record_is_set_aside_not_fatal(self):
        # 2026-10-15: a run.json with a raw NUL (a shared tmp file truncated under another writer) made every later
        # desk command on that ticket raise in json.load; the ticket was wedged until a human removed the file.
        d, c = mk()
        rd = os.path.join(d.root, "runs", "8"); os.makedirs(rd)
        with open(os.path.join(rd, "run.json"), "w") as fh:
            fh.write('{"ticket": "8", "phase": "investigate", "agents": [{"label": "inv1", "x": "a\x00\x00b"}]}')
        r = d.wait("8", timeout_s=0.3, poll_s=0.1)
        self.assertEqual(r["phase"], "new", r)
        self.assertTrue([n for n in os.listdir(rd) if n.startswith("run.json.corrupt-")], os.listdir(rd))
        self.assertEqual(d.trace("8")["run"]["phase"], "new")

    def test_concurrent_writers_never_leave_a_torn_record(self):
        import threading
        d, c = mk()
        path = os.path.join(d.root, "runs", "7", "run.json")
        errors = []
        def w(tag):
            try:
                for i in range(60):
                    D.write_json(path, {"ticket": "7", "phase": "investigate", "pad": tag * 3000, "i": i}, indent=1)
                    D.read_json(path, quarantine=False)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=w, args=(t,)) for t in ("a", "b", "c", "d")]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(D.read_json(path, quarantine=False)["ticket"], "7")
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], "no temp file left behind")

    def test_one_dash_is_one_hit(self):
        d, c = mk()
        r = d.file("4", "Hi,\n\nThe broker is off \u2014 turn it on.\n")
        self.assertEqual([h for h in r["hits"] if "dash" in h], ["dash"])



class FleetParityTests(unittest.TestCase):
    """The fleet's send gates the desk carries, so a draft that clears the desk clears the fleet's usual bar."""

    def test_requester_name_unrecorded_mismatch_and_match(self):
        d, c = mk(requesters=False)
        r = d.file("41", "Hi Alvar,\n\nPlease send a fresh support package.\n")
        self.assertIn("requester-name-unrecorded", r["hits"])
        d.requester("41", "Alba Ruiz")
        r = d.file("41", "Hi Alvar,\n\nPlease send the support package again.\n")
        self.assertIn("requester-name-mismatch", r["hits"])
        self.assertEqual(d._requests()[r["md5"]]["checks"]["requester"], {"greeted": "Alvar", "recorded": "Alba Ruiz"})
        r = d.file("41", "Hi Alba,\n\nPlease send a new support package.\n")
        self.assertNotIn("requester-name-mismatch", r["hits"])
        self.assertNotIn("requester-name-unrecorded", r["hits"])
        r = d.file("41", "Please send one more support package.\n")     # no greeting, nothing to check
        self.assertNotIn("requester", d._requests()[r["md5"]]["checks"])
        d.requester("42", "Connick Shields")
        self.assertEqual(d._requesters()["42"]["name"], "Connick Shields")

    def test_a_deny_on_the_ticket_holds_the_next_draft_for_72h(self):
        d, c = mk()
        m = d.file("43", "Please send a fresh support package.\n")["md5"]
        d.deny(m, "wrong product")
        r = d.file("43", "Please send the support package from the sensor host.\n")
        self.assertIn("ticket-denied-recently", r["hits"])
        self.assertEqual(d._requests()[r["md5"]]["checks"]["denied_recently"][0]["reason"], "wrong product")
        self.assertNotIn("ticket-denied-recently", d.file("44", "Please send a fresh support package.\n")["hits"])
        c.t += 73 * 3600
        self.assertNotIn("ticket-denied-recently", d.file("43", "Please send the support package again today.\n")["hits"])

    def test_modify_is_not_a_deny(self):
        d, c = mk()
        m = d.file("45", "Please send a fresh support package.\n")["md5"]
        d.modify(m, "shorter")
        self.assertNotIn("ticket-denied-recently", d.file("45", "Please send a support package.\n", supersedes=m)["hits"])

    def test_sends_held_switch(self):
        d, c = mk()
        open(os.path.join(d.root, "sends-held"), "w").close()
        self.assertIn("sends-held", d.file("46", "Please send a fresh support package.\n")["hits"])

    def test_escalation_needs_a_recorded_mismatch(self):
        d, c = mk()
        T = "47"
        d.capture(T, [{"id": "1", "who": "customer", "at": T0 - 3600, "text": "The scan finished but the connector still shows as unhealthy after we enabled the broker."}], 1)
        d.page("https://docs.example.org/broker", "Broker\n\nWhen the broker is enabled the connector reports healthy after the next scan.\n")
        docs = d.receipt(T, "docs", "When the broker is enabled the connector reports healthy after the next scan", "https://docs.example.org/broker")["id"]
        seen = d.receipt(T, "material", "the connector still shows as unhealthy after we enabled the broker")["id"]
        draft = "I've passed along your request to the WizOS engineering team.\n"
        r = d.file(T, draft)
        self.assertIn("escalation-without-mismatch", r["hits"])
        self.assertEqual(d.mismatch(T, seen, docs, "docs say healthy, it is not")["why"], "documented-not-a-docs-receipt")
        self.assertEqual(d.mismatch(T, docs, docs, "docs say healthy, it is not")["why"], "observed-not-a-material-or-test-receipt")
        self.assertEqual(d.mismatch(T, docs, seen, "differs")["why"], "summary-missing")
        self.assertTrue(d.mismatch(T, docs, seen, "the docs say healthy after a scan; theirs stays unhealthy")["ok"])
        d.modify(r["md5"], "retry")
        r = d.file(T, draft)
        self.assertNotIn("escalation-without-mismatch", r["hits"])
        self.assertEqual(len(d._requests()[r["md5"]]["checks"]["escalation"]["mismatch"]), 1)
        d.modify(r["md5"], "again")
        c.t += 25 * 3600                                      # the receipts behind it went stale: no basis
        self.assertIn("escalation-without-mismatch", d.file(T, "We escalated this to engineering.\n")["hits"])

    def test_fleet_reversal_phrases_hold(self):
        import thread_review as TR
        for s in ("Apologies, the earlier steps were for the other connector.", "My prior response named the wrong page.",
                  "I got that wrong in the last note.", "I was wrong to suggest a restart."):
            self.assertTrue(TR.REVERSAL.search(s), s)

    def test_partial_binding_at_the_fleets_thresholds(self):
        import claim_receipts as CR
        self.assertEqual((CR.POLICY["unbound_words"], CR.POLICY["unbound_share"]), (3, 0.2))

    def test_echo_share_is_a_warning(self):
        d, c = mk()
        T = "48"
        said = "we upgraded the sensor to the newest release last week and since then the console shows no data at all"
        d.capture(T, [{"id": "1", "who": "customer", "at": T0 - 3600, "text": said}], 1)
        r = d.file(T, "You upgraded the sensor to the newest release last week and since then the console shows no data at all. Please send a support package.\n")
        self.assertGreaterEqual(r["claims"]["echo_share"], 0.25)
        self.assertIn("echo-share", r["warnings"])


    def test_customer_cannot_act_and_holding_reply(self):
        d, c = mk()
        d.capture("49", [{"id": "1", "who": "customer", "at": T0 - 3600, "text": "I'm confused, what do you want me to do?"}], 1)
        self.assertIn("customer-cannot-act", d.file("49", "Could you confirm which version you run?\n")["hits"])
        self.assertNotIn("customer-cannot-act", d.file("49", "1. Open Settings > Connectors.\n2. Enable the broker.\n")["hits"])
        d.capture("50", [{"id": "1", "who": "customer", "at": T0 - 3600, "text": "Which version fixes this?"}], 1)
        self.assertIn("holding-reply", d.file("50", "We're still checking this.\n")["hits"])
        self.assertNotIn("holding-reply", d.file("50", "We're still checking this, and I'm reproducing it on our side.\n")["hits"])

    def test_open_objection_that_quotes_the_draft_holds_until_answered(self):
        d, c = mk()
        m = d.file("51", "Please send a fresh support package from the sensor host.\n")["md5"]
        c.t += 5
        d.finding("51", "revA", "reviewer", "objection", "[misattributed] the draft says send a fresh support package from the sensor host but they sent one", about=m)
        d.finding("51", "revB", "reviewer", "objection", "[question-not-answered] nothing in this reply is about their question", about=m)   # quotes nothing: discarded
        d.finding("51", "op", "operator", "objection", "Modify: send a fresh support package from the sensor host", about=m)
        it = d.list()["items"][0]
        self.assertEqual(it["checks"]["review"], ["review:misattributed"])
        self.assertEqual(it["checks"]["verdict"], "hold")
        oid = [f for f in d._rows(os.path.join(d._run_dir("51"), "findings.jsonl")) if f["agent"] == "revA"][0]["id"]
        d.finding("51", "drafter", "drafter", "answer", "they sent it for the other host", about=oid)
        self.assertNotIn("review", d.list()["items"][0]["checks"])
        d.finding("51", "revA", "reviewer", "objection", "[made-up] send a fresh support package from the sensor host is wrong", about=m)
        g = d.grant(m, "press-51")
        self.assertIn("review:objection", g["overruled"])


    def test_filed_row_lists_the_receipts_the_fleet_check_re_mints(self):
        d, c = mk()
        T = "52"
        d.page("https://docs.example.org/broker", "Broker\n\nWhen the broker is disabled the connector reports unhealthy.\n")
        rid = d.receipt(T, "docs", "When the broker is disabled the connector reports unhealthy", "https://docs.example.org/broker")["id"]
        m = d.file(T, "When the broker is disabled the connector reports unhealthy.\n")["md5"]
        fr = d._requests()[m]["fleet_receipts"]
        self.assertEqual(fr, [{"sentence": "When the broker is disabled the connector reports unhealthy.", "fleet_kind": "docs",
                               "url": "https://docs.example.org/broker", "quote": "When the broker is disabled the connector reports unhealthy",
                               "desk_receipt": rid}])


    def test_a_customer_message_after_filing_holds_the_draft(self):
        d, c = mk()
        T = "53"
        d.capture(T, [{"id": "1", "who": "customer", "at": T0 - 3600, "text": "All sensors are latest version."}], 1)
        m = d.file(T, "Please send the sensor version from the host.\n")["md5"]
        self.assertNotIn("thread-moved", d.list()["items"][0]["checks"]["hits"])
        c.t += 600
        d.capture(T, [{"id": "1", "who": "customer", "at": T0 - 3600, "text": "All sensors are latest version."},
                      {"id": "2", "who": "customer", "at": c.t - 60, "text": "Does the runtime sensor auto update?"}], 2)
        it = d.list()["items"][0]
        self.assertIn("thread-moved", it["checks"]["hits"])
        self.assertEqual(it["checks"]["verdict"], "hold")
        self.assertIn("auto update", it["checks"]["moved"][0]["text"])
        self.assertIn("thread-moved", d.grant(m, "press-53")["overruled"])

    def test_quote_markers_hold(self):
        d, c = mk()
        self.assertIn("quote-markers", d.file("54", "> Please send the sensor version.\n> Thanks.\n")["hits"])
        self.assertNotIn("quote-markers", d.file("55", "Please send the sensor version (the one > 1.2).\n")["hits"])

if __name__ == "__main__":
    unittest.main()
