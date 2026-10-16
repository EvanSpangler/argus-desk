import io, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import desk_mcp as M

DRAFT = "Hi Sam,\n\nThe broker for this connector is disabled, so the connector reports unhealthy. Re-enable the broker in the portal.\n\nThanks!\n"


def talk(server_root, role, messages):
    """Feed JSON-RPC lines to the server and return its answers, one per request id."""
    inp = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    out = io.StringIO()
    M.serve(M.Desk(server_root, "docs.wiz.io"), role, inp=inp, out=out)
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


class McpTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_handshake_and_tool_list(self):
        res = talk(self.root, "investigator", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        ])
        self.assertEqual(res[0]["result"]["serverInfo"]["name"], "argus-desk")
        self.assertEqual([t["name"] for t in res[1]["result"]["tools"]], ["file_draft", "skip_ticket", "desk_finding", "desk_receipt", "desk_mismatch"])
        self.assertEqual(res[2]["error"]["code"], -32601, "an unknown method is refused, not ignored")
        self.assertEqual(len(res), 3, "the notification gets no answer")

    def test_finding_and_refused_receipt_land_in_the_same_record(self):
        res = talk(self.root, "investigator", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "desk_finding", "arguments": {"ticket": "123456", "kind": "hypothesis", "text": "The broker is disabled."}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "123456", "kind": "docs", "quote": "A disabled broker reports unhealthy.", "source": "https://docs.wiz.io/x"}}},
        ])
        self.assertFalse(res[0]["result"]["isError"], res[0])
        self.assertRegex(res[0]["result"]["content"][0]["text"], r"^Recorded .+ \(hypothesis\)\.$")
        self.assertTrue(res[1]["result"]["isError"])
        self.assertIn("page-not-captured", res[1]["result"]["content"][0]["text"])
        self.assertIn("fetch the page in this run first", res[1]["result"]["content"][0]["text"])
        # the same desk.py store the hooks write: the finding is on the ticket's record
        import desk as D
        trace = D.Desk(self.root).trace("123456")
        kinds = [f.get("kind") for f in trace.get("findings", [])]
        self.assertIn("hypothesis", kinds, trace)
        self.assertEqual(trace["findings"][0].get("agent"), "investigator", "the role is the recorded author")

    def test_receipt_fault_a_handler_error_is_the_tools_answer_not_the_servers_end(self):
        # 2026-10-15: one desk_finding "failed with Connec[tion closed]" and the agent lost every later call
        real = M.handle
        def boom(desk, role, name, a):
            if name == "desk_finding":
                raise RuntimeError("boom \u2013 dash")
            return real(desk, role, name, a)
        M.handle = boom
        try:
            res = talk(self.root, "investigator", [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "desk_finding", "arguments": {"ticket": "1", "kind": "summary", "text": "x"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "1", "kind": "material", "quote": "caf\u00e9 \u2013 words"}}},
            ])
        finally:
            M.handle = real
        self.assertEqual(len(res), 2, res)
        self.assertTrue(res[0]["result"]["isError"]); self.assertIn("RuntimeError", res[0]["result"]["content"][0]["text"])
        self.assertIn("no-thread-captured", res[1]["result"]["content"][0]["text"])
        self.assertIn("read every comment", res[1]["result"]["content"][0]["text"])

    def test_receipt_fault_refusals_carry_what_the_desk_holds(self):
        import desk as D
        d = D.Desk(self.root)
        d.capture("7", [{"id": "1", "who": "customer", "at": 1.0, "text": "All sensors are latest version. Please assist."}], 1)
        D.CR.POLICY["docs_hosts"].append("docs.wiz.io")
        d.page("https://docs.wiz.io/docs/sensor-versions", "The sensor updates itself when a new version is published.", origin="search")
        res = talk(self.root, "investigator", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "7", "kind": "material", "quote": "All sensors are latest version."}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "7", "kind": "material", "quote": "not their words"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "7", "kind": "docs", "quote": "The sensor updates itself when a new version is published", "source": "docs.wiz.io/docs/sensor-versions/"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "desk_receipt", "arguments": {"ticket": "7", "kind": "docs", "quote": "anything", "source": "https://docs.wiz.io/docs/elsewhere"}}},
        ])
        self.assertFalse(res[0]["result"]["isError"], res[0])
        self.assertIn("1 customer message(s)", res[1]["result"]["content"][0]["text"])
        self.assertFalse(res[2]["result"]["isError"], res[2])
        self.assertIn("captured so far: https://docs.wiz.io/docs/sensor-versions", res[3]["result"]["content"][0]["text"])

    def test_file_draft_files_and_marks_drafting(self):
        res = talk(self.root, "drafter", [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "file_draft", "arguments": {"ticket": "123456", "draft": DRAFT, "citations": [{"sentence": "The broker for this connector is disabled, so the connector reports unhealthy.", "receipt": "r-none"}]}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "skip_ticket", "arguments": {"ticket": "123457", "reason": "customer answered themselves"}}},
        ])
        text = res[0]["result"]["content"][0]["text"]
        self.assertFalse(res[0]["result"]["isError"], text)
        self.assertRegex(text, r"^Filed draft [0-9a-f]{32} for #123456\. Checks: ")
        self.assertIn("unknown receipts: r-none", text)
        self.assertIn("waiting for the operator", text)
        import desk as D
        d = D.Desk(self.root)
        self.assertEqual(d.list()["items"][0]["state"], "pending")
        rows = {r["ticket"]: r for r in d.queue_view()["rows"]}
        # the queue view shows a ticket with a draft waiting for the operator as `waiting` (the hook's mark drafting is the same path)
        self.assertEqual(rows["123456"]["state"], "waiting")
        self.assertEqual(rows["123457"]["state"], "skipped")
        self.assertIn("needs no reply now", res[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
