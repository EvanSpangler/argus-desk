import io, os, sys, json, tempfile, unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import desk as D
import desk_tree as DT

T0 = 1_800_000_000.0

class Clock:
    def __init__(self, t=T0): self.t = t
    def __call__(self): return self.t

DRAFT = "Hi Sam,\n\nThe connector reports unhealthy because the broker is disabled. Re-enable the broker under Settings > Connectors, then run a fresh scan.\n\nThanks!\n"

def seed():
    """A ticket with a recorded thread, two investigators, a docs and a material receipt, one finding, one open
    objection that quotes the second sentence, and a filed draft citing both receipts."""
    root = tempfile.mkdtemp(); c = Clock(); d = D.Desk(root, clock=c); T = "12"
    d.capture(T, [{"id": "1", "who": "customer", "at": T0 - 7200, "text": "Our connector reports unhealthy since Monday. Which agent version is required?"}], 1)
    d.run_update(T, phase="new", agent={"id": "a1", "role": "investigator", "label": "inv1", "model": "sonnet", "status": "running", "prompt": "read the docs", "params": "docs-first"})
    d.run_update(T, agent={"id": "a3", "role": "investigator", "label": "inv3", "model": "sonnet", "status": "completed", "params": "thread-first", "summary": "They ask which agent version; the connector is unhealthy."})
    d.page("https://docs.example.org/broker", "Broker health\n\nWhen the broker is disabled the connector reports unhealthy. Re-enable the broker under Settings > Connectors to restore scanning.\n")
    r1 = d.receipt(T, "docs", "Re-enable the broker under Settings > Connectors", "https://docs.example.org/broker", by="inv1")
    r2 = d.receipt(T, "material", "connector reports unhealthy", by="inv1")
    d.call(T, "inv1", "invoke_proxy", {"proxy": "zendesk-full", "endpoint": "zendesk_list_comments"}, result="x")
    f1 = d.finding(T, "inv1", "investigator", "evidence", "The broker is disabled, so the connector reports unhealthy", receipts=[r1["id"], r2["id"]])
    o1 = d.finding(T, "revB", "reviewer", "objection", "The sentence 'Re-enable the broker under Settings > Connectors' names a path the receipt does not confirm")
    d.run_update(T, agent={"id": "a1", "status": "completed", "answer": "done"})
    d.cite(T, [{"receipt": r1["id"], "sentence": "Re-enable the broker under Settings > Connectors, then run a fresh scan."},
               {"receipt": r2["id"], "sentence": "The connector reports unhealthy because the broker is disabled."}])
    f = d.file(T, DRAFT)
    return d, c, T, f["md5"], {"r1": r1["id"], "r2": r2["id"], "f1": f1["id"], "o1": o1["id"]}

def ids(node):
    return [c["id"] for c in node["children"]]

class TreeTests(unittest.TestCase):
    def test_sentences_carry_legs_receipts_findings_and_arguments(self):
        d, c, T, md5, k = seed()
        t = DT.build(d, md5)
        self.assertTrue(t["ok"], t)
        self.assertEqual(ids(t["tree"]), ["issues", "ask", "reply", "debate", "run", "send"])
        reply = DT.find(t["tree"], "reply")
        marks = [(s["mark"], s["state"]) for s in reply["children"]]
        self.assertEqual(marks, [("-", "asserts-nothing"), ("material", "problem"), ("docs", "ok"), ("-", "asserts-nothing")])
        s2 = DT.find(t["tree"], "reply:s2")
        self.assertEqual([x["id"] for x in s2["children"]], ["reply:s2:r:" + k["r1"].replace("rcpt:", ""), "reply:s2:carried", "reply:s2:argued"])
        rcpt = s2["children"][0]
        self.assertIn("(fetched page)", rcpt["label"]); self.assertIn("minted by inv1 (sonnet)", rcpt["body"])
        self.assertEqual(rcpt["target"], "run:inv1")
        self.assertIn("Re-enable the broker under Settings > Connectors to restore scanning", rcpt["children"][0]["body"])
        s1 = DT.find(t["tree"], "reply:s1")
        self.assertEqual(len([x for x in s1["children"] if x["id"].startswith("reply:s1:r:")]), 1, "a receipt bound twice is one node")
        self.assertIn("the whole message", s1["children"][0]["children"][0]["label"])
        carried = DT.find(t["tree"], "reply:s1:carried")
        self.assertIn(k["f1"], carried["children"][0]["id"])
        argued = DT.find(t["tree"], "reply:s2:argued")
        self.assertEqual(argued["children"][0]["state"], "open")

    def test_issues_point_at_their_targets_and_close_as_the_record_moves(self):
        d, c, T, md5, k = seed()
        t = DT.build(d, md5)
        issues = DT.find(t["tree"], "issues")
        self.assertEqual(t["open"], 2)
        by_id = {i["id"]: i for i in issues["children"]}
        self.assertEqual(by_id["issue:" + k["o1"]]["target"], "reply:s2", "the objection quotes the second sentence")
        self.assertEqual(by_id["issue:s1:cause-needs-product-leg-and-material"]["target"], "reply:s1")
        d.finding(T, "drafter", "drafter", "answer", "The docs page names Settings > Connectors verbatim", about=k["o1"])
        t = DT.build(d, md5)
        self.assertEqual(t["open"], 1)
        self.assertEqual(DT.find(t["tree"], "debate:" + k["o1"])["state"], "answered")
        self.assertEqual(len(DT.find(t["tree"], "debate:" + k["o1"])["children"]), 1)
        self.assertTrue(d.grant(md5, "press-1")["ok"])
        t = DT.build(d, md5)
        self.assertEqual(t["state"], "open")
        self.assertIn("SEND at", DT.find(t["tree"], "send:decision")["label"])
        self.assertIn("OVERRULED cause-needs-product-leg-and-material", DT.find(t["tree"], "send:decision")["label"])
        self.assertEqual(DT.find(t["tree"], "send:status")["label"], "status it sets: pending")

    def test_a_ticket_updated_after_the_record_is_an_issue_on_the_thread(self):
        d, c, T, md5, k = seed()
        self.assertIsNone(DT.find(DT.build(d, md5)["tree"], "issue:stale"))
        later = datetime.fromtimestamp(T0 + 600, timezone.utc).isoformat().replace("+00:00", "Z")
        d._mark(T, "waiting", updated=later)
        t = DT.build(d, md5)
        self.assertEqual(DT.find(t["tree"], "issue:stale")["target"], "ask:thread")
        self.assertEqual(DT.find(t["tree"], "ask:thread")["state"], "stale")
        self.assertIn("TICKET UPDATED SINCE THE RECORD", DT.find(t["tree"], "ask:thread")["label"])
        self.assertEqual(DT.find(t["tree"], "ask:questions")["children"][0]["label"], "Which agent version is required?")
        self.assertEqual(DT.find(t["tree"], "ask:summary")["target"], "run:inv3")

    def test_run_nodes_load_prompts_on_demand_and_the_text_rendering_matches(self):
        d, c, T, md5, k = seed()
        t = DT.build(d, md5)
        p = DT.find(t["tree"], "run:inv1:prompt")
        self.assertNotIn("body", p); self.assertEqual(p["chars"], 13)
        self.assertEqual(DT.load_body(d, p)["body"], "read the docs")
        self.assertEqual(DT.find(t["tree"], "run:inv1:calls")["label"], "1 calls, 0 refused receipts")
        text = "\n".join(DT.render(t["tree"]))
        self.assertIn("- ISSUES: 2 open", text)
        self.assertIn("[docs] Re-enable the broker", text)
        lines = text.splitlines()
        at = next(i for i, l in enumerate(lines) if "[docs] Re-enable the broker" in l)
        self.assertIn("- receipt", lines[at + 1], "a body that repeats its label is not printed under it")
        out = io.StringIO()
        with redirect_stdout(out):
            D.main(["tree", "--root", d.root, "--ticket", T, "--text", "--depth", "1"])
        self.assertIn("- REPLY: 4 sentences", out.getvalue()); self.assertNotIn("[docs]", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            D.main(["tree", "--root", d.root, "--md5", md5, "--node", "run:inv1:answer"])
        self.assertEqual(json.loads(out.getvalue())["tree"]["body"], "done")

    def test_queue_rows_carry_what_the_tickets_pane_opens(self):
        d, c, T, md5, k = seed()
        d._mark(T, "drafting")
        row = next(r for r in d.queue_view()["rows"] if r["ticket"] == T)
        self.assertEqual(row["state"], "waiting")
        self.assertEqual(row["thread"], {"messages": 1, "count": 1, "fetched_at": T0})
        self.assertEqual(row["run"], {"phase": "new", "rounds": 0, "agents": 2})
        self.assertEqual(row["drafts"], [md5])

    def test_unknown_draft_and_missing_thread(self):
        d, c = D.Desk(tempfile.mkdtemp(), clock=Clock()), None
        self.assertFalse(DT.build(d, "nope")["ok"])
        self.assertFalse(DT.build(d, ticket="9")["ok"])
        md5 = d.file("9", DRAFT)["md5"]
        t = DT.build(d, md5)
        self.assertEqual(DT.find(t["tree"], "ask:thread")["state"], "missing")
        self.assertEqual(DT.find(t["tree"], "reply:s1")["state"], "uncovered")
        self.assertTrue(any(i["id"] == "issue:s1:uncovered" for i in DT.find(t["tree"], "issues")["children"]))

if __name__ == "__main__":
    unittest.main()
