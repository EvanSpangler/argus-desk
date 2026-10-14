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
        self.assertTrue(d.consume(["unrelated text", DRAFT], "post")["ok"])
        self.assertFalse(d.consume([DRAFT], "post again")["ok"], "one grant, one send")
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
        self.assertTrue(run(["consume"], json.dumps({"candidates": [DRAFT], "what": "POST comments"}))["ok"])
        self.assertEqual(run(["stats"])["decisions"]["send"], 1)

if __name__ == "__main__":
    unittest.main()
