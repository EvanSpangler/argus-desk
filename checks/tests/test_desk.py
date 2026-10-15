import os, sys, json, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import desk as D

T0 = 1_800_000_000.0

class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t

def mk():
    root = tempfile.mkdtemp()
    c = Clock()
    return D.Desk(root, clock=c), c

FLAT = '{"endpoint":"zendesk_update_ticket","arguments":{"ticket":{"status":"pending","comment":{"body":"..."}}}}'
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
        self.assertTrue(d.consume(["unrelated text", DRAFT], "post", FLAT)["ok"])
        self.assertFalse(d.consume([DRAFT], "post again", FLAT)["ok"], "one grant, one send")
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

    def test_press_id_is_single_spend(self):
        d, c = mk()
        a = d.file("3", DRAFT)["md5"]; b = d.file("3", DRAFT + "x\n")["md5"]
        self.assertTrue(d.grant(a, "same-press")["ok"])
        self.assertFalse(d.grant(b, "same-press")["ok"])

    def test_lint_holds_dash_and_timeline(self):
        d, c = mk()
        r = d.file("4", "Hi,\n\nI will get back to you shortly — the team is looking.\n")
        self.assertEqual(r["verdict"], "hold"); self.assertIn("dash", r["hits"]); self.assertIn("timeline-commitment", r["hits"])

    def test_thread_capture_drives_cadence_and_questions(self):
        d, c = mk()
        d.capture("5", [{"id": "1", "who": "customer", "at": T0 - 7200, "text": "Scans fail. Which agent version is required?"},
                        {"id": "2", "who": "us", "at": T0 - 3600, "text": "Please send the logs."}], count=2)
        r = d.file("5", DRAFT)
        self.assertEqual(r["thread"]["status"], "captured")
        self.assertIn("too-soon", r["hits"])
        self.assertIn("repeat-ask", r["hits"])
        self.assertEqual(r["thread"]["open_questions"], [])  # question predates our last send

    def test_unrecorded_comment_holds(self):
        d, c = mk()
        d.capture("6", [{"id": "1", "who": "customer", "at": T0 - 90000, "text": "Hello?"}], count=3)
        r = d.file("6", DRAFT)
        self.assertIn("unrecorded-comment", r["hits"])

    def test_claims_are_shadow_counts(self):
        d, c = mk()
        r = d.file("7", DRAFT)
        self.assertEqual(r["claims"]["credited"], 0)
        self.assertGreater(r["claims"]["checkable"], 0)
        self.assertEqual(r["verdict"], "clear", "claims do not hold in v0")

    def test_cli_roundtrip(self):
        import subprocess
        root = tempfile.mkdtemp(); here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "desk.py")
        run = lambda args, inp=None: json.loads(subprocess.run([sys.executable, "-I", here, "--root", root] + args, input=inp, capture_output=True, text=True).stdout)
        r = run(["file"], json.dumps({"ticket": "8", "draft": DRAFT}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(run(["list"])["items"]), 1)
        self.assertTrue(run(["grant", "--md5", r["md5"], "--press-id", "p"])["ok"])
        self.assertTrue(run(["consume"], json.dumps({"candidates": [DRAFT], "what": "POST comments", "flat": FLAT}))["ok"])
        self.assertEqual(run(["stats"])["decisions"]["send"], 1)

    def test_grant_status_must_be_on_the_posting_call(self):
        d, c = mk()
        md5 = d.file("123456", DRAFT)["md5"]
        self.assertTrue(d.grant(md5, "press-1", status="solved")["ok"])
        r = d.consume([DRAFT], "post", '{"arguments":{"comment":{"body":"x"}}}')
        self.assertEqual(r.get("code"), "status-missing"); self.assertEqual(r["status"], "solved")
        self.assertEqual(d.list()["items"][0]["state"], "open", "the grant is left open")
        r = d.consume([DRAFT], "post", '{"arguments":{"status":"pending"}}')
        self.assertEqual(r.get("code"), "status-missing", "the wrong status is not the status")
        r = d.consume([DRAFT], "post", '{"arguments":{"status": "solved"}}')
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
        d.grant(md5, "p1"); d.consume([DRAFT], "post", FLAT)
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

if __name__ == "__main__":
    unittest.main()
