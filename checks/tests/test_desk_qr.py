import os, sys, json, hashlib, tempfile, unittest, io
from contextlib import redirect_stdout
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import desk as D
import desk_qr as Q

TEXT = "Hello,\n\nThe Workload Scanner is a separate component: https://app.wiz.io/settings/deployments#~(view~'list)%7E%29\n\nBest, Evan\n"
MD5 = hashlib.md5(TEXT.encode()).hexdigest()

def read_back(lines):
    """The half-block lines back to module rows (True = dark), then to the payload, through the vendored encoder's own
    matrix: the drawing must be exactly the matrix, two rows a line."""
    top = {"█": False, "▀": False, "▄": True, " ": True}
    bot = {"█": False, "▀": True, "▄": False, " ": True}
    rows = []
    for l in lines:
        rows.append([top[c] for c in l]); rows.append([bot[c] for c in l])
    return rows

class QrTests(unittest.TestCase):
    def test_codes_fit_the_pane_and_draw_the_matrix_exactly(self):
        r = Q.codes(MD5, TEXT, 72)
        self.assertTrue(r["ok"])
        for c in r["codes"]:
            self.assertTrue(all(len(l) <= 72 for l in c["lines"]))
            m = Q.matrix(c["payload"], r["version"])
            self.assertEqual(read_back(c["lines"])[:len(m)], m)

    def test_join_returns_the_exact_text_in_any_order_with_repeats(self):
        p = [c["payload"] for c in Q.codes(MD5, TEXT, 50)["codes"]]
        self.assertGreater(len(p), 1)
        j = Q.join(list(reversed(p)) + p[:1])
        self.assertTrue(j["ok"]); self.assertEqual(j["text"], TEXT)
        self.assertEqual(hashlib.md5(j["text"].encode()).hexdigest(), MD5)

    def test_a_missing_or_misread_slice_is_refused_never_mistaken_for_the_draft(self):
        p = [c["payload"] for c in Q.codes(MD5, TEXT, 50)["codes"]]
        miss = Q.join(p[1:])
        self.assertFalse(miss["ok"]); self.assertEqual(miss["missing"], [1])
        bad = p[0][:-4] + ("AAAA" if not p[0].endswith("AAAA") else "BBBB")
        self.assertFalse(Q.join([bad] + p[1:])["ok"])
        self.assertFalse(Q.join(["not a desk code"])["ok"])

    def test_text_that_is_not_the_md5_draws_nothing(self):
        self.assertFalse(Q.codes(MD5, TEXT + "x", 72)["ok"])

    def test_cli_qr_draws_a_filed_draft_by_md5(self):
        root = tempfile.mkdtemp()
        f = D.Desk(root).file("12", TEXT)
        out = io.StringIO()
        with redirect_stdout(out):
            D.main(["qr", "--root", root, "--md5", f["md5"], "--width", "60"])
        r = json.loads(out.getvalue())
        self.assertTrue(r["ok"]); self.assertEqual(r["md5"], f["md5"])
        self.assertEqual(Q.join([c["payload"] for c in r["codes"]])["text"], TEXT)
        with redirect_stdout(io.StringIO()) as o2:
            D.main(["qr", "--root", root, "--md5", "0" * 32])
        self.assertFalse(json.loads(o2.getvalue())["ok"])

if __name__ == "__main__":
    unittest.main()
