"""claim_receipts: reference implementation of the two-leg claim check for outbound replies.

Every checkable sentence of a draft needs a receipt of one of two kinds:
  docs  a verbatim quote from official documentation, found in PROSE of a page body the harness
        fetched itself (never a code block, never an example), and the quote must support the sentence
  test  recorded output of something actually run in a test environment, captured by the harness
A third kind, "material", is the customer's own data. It is supporting evidence about THEIR system
and can never prove how the product behaves, so it is not a leg.
A claim is verified or it is absent: hedged "unconfirmed" wording never clears anything.
Stdlib only. Time is injected for tests.
"""
import hashlib
import html as _html
import json
import os
import re
from datetime import datetime, timedelta, timezone

# unbound_words/unbound_share are the fleet's (claim-check max_unbound_words 3, max_unbound_share 0.2): a desk draft
# that clears here must clear there. echo_ngram is its draft-level copy-back measure (a warning, as in the fleet).
POLICY = {"max_age_hours": 24, "support_share": 0.5, "weak_min_words": 5, "unbound_words": 3,
          "unbound_share": 0.2, "echo_share": 0.6, "echo_ngram": 6, "echo_warn_share": 0.25, "docs_hosts": ["docs.example.org"],
          "good_origins": ["captured_text", "ocr_of_capture"]}

STOP = set("a an the and or but if then than that this these those it its is are was were be been to of in on "
           "at by for with as from into your you our we i they them their can will would should could do does "
           "not no so also just have has had there here when what which who".split())
_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}


def norm(s):
    s = "".join(_QUOTES.get(c, c) for c in str(s or ""))
    return re.sub(r"\s+", " ", s.lower()).strip()


def plain(s):
    """Text out of a comment body that may be HTML: tags dropped, entities unescaped. A customer's words quoted from
    the console are plain; the proxy may hand the agent html_body, and the two must compare equal."""
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", str(s or ""), flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return _html.unescape(t)


def squash(s):
    return re.sub(r"[^a-z0-9]", "", norm(s))


def content_words(s):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]*", norm(s)) if w not in STOP and len(w) > 1}


# ---------------------------------------------------------------- what counts as a claim
FENCE = re.compile(r"^\s*```")
INDENT = re.compile(r"^( {4,}|\t)\S")
BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(`A-Z0-9])")
GREETING = re.compile(r"^(hi|hello|hey|dear|good (morning|afternoon|evening))\b[^.!?]{0,60}[,!.]?$", re.I)
PLEASANT = re.compile(r"^(thanks|thank you|appreciate|happy to help|hope (this|that) helps)\b", re.I)
ASK = re.compile(r"\?$|^(can|could|would|will|do|did|is|are)\s+you\b|^please\b", re.I)
COMMIT = re.compile(r"^(i'll|i will|we'll|we will|i'm going to)\b", re.I)
ELEMENT = re.compile(r"`[^`]+`|(?:^|\s)--?[a-z][\w-]*|[\w-]+\.(?:ya?ml|json|tf|toml|conf|cfg|log|sh|py)\b|"
                     r"\b[A-Z][\w]*(?:\s*>\s*[A-Z][\w]*)+|\bv?\d+\.\d+(?:\.\d+)?\b|"
                     r"\b\d+\s*(?:days?|hours?|minutes?|seconds?|gb|mb|%)", re.I)
INSTR = re.compile(r"^(?:(?:to|if|once|after|before|when)\b[^,:]{2,80}[,:]\s*)?(?:then |first,? |also )?"
                   r"(?:enable|disable|set|run|open|click|select|add|remove|delete|update|upgrade|install|"
                   r"restart|replace|use|configure|create|go to|navigate|turn)\b|"
                   r"\byou (?:can|could|should|must|need to|will need to)\b", re.I)
CAUSE = re.compile(r"\b(because|due to|caused by|root cause|the reason|which is why|that's why|results? from)\b", re.I)
STATUS = re.compile(r"\b(fixed|resolved|released|deployed|rolled out|shipped|in progress|scheduled|planned)\b", re.I)
BEHAVE = re.compile(r"\b(supports?|requires?|allows?|enables?|defaults? to|returns?|cannot|can't|does not|"
                    r"doesn't|will not|won't|is (?:not )?(?:supported|available|enabled|disabled|required))\b", re.I)
ENV = re.compile(r"\b(your|you have|you ran|you sent|you shared|you configured)\b", re.I)
HEDGE = re.compile(r"\b(may|might|could be|possibly|probably|likely|appears? to|seems? to|i suspect|i believe)\b", re.I)
UNCONFIRMED = re.compile(r"\b(unconfirmed|unverified|not (?:yet )?(?:confirmed|verified)|haven't (?:confirmed|verified)|"
                         r"have not (?:confirmed|verified)|don't have a confirmed|cannot confirm|can't confirm)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|cannot|can't|doesn't|don't|won't|isn't|aren't|without)\b", re.I)
CONDITION = re.compile(r"\b(only if|only when|unless|requires?|if and only if|provided that)\b", re.I)


def units(text):
    out, para = [], []

    def flush():
        if para:
            for s in SPLIT.split(" ".join(p.strip() for p in para)):
                if s.strip():
                    out.append(("sentence", s.strip()))
            para.clear()
    lines, i = str(text or "").replace("\r\n", "\n").split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if FENCE.match(ln):
            flush()
            j = i + 1
            while j < len(lines) and not FENCE.match(lines[j]):
                j += 1
            out.append(("snippet", "\n".join(lines[i + 1:j])))
            i = j + 1
            continue
        if INDENT.match(ln):
            flush()
            out.append(("snippet", ln.strip()))
        elif not ln.strip():
            flush()
        elif BULLET.match(ln):
            flush()
            para.append(BULLET.sub("", ln))
            flush()
        else:
            para.append(ln)
        i += 1
    flush()
    return out


def classify(s):
    """Return the claim classes of a sentence; an empty list means it asserts nothing checkable."""
    has_el = bool(ELEMENT.search(s))
    if GREETING.match(s) or (PLEASANT.match(s) and not has_el and len(s.split()) <= 12):
        return []
    if ASK.search(s) or COMMIT.match(s):
        return ["element"] if has_el else []   # an ask that names a thing asserts the thing exists
    cls = [k for k, rx in (("instruction", INSTR), ("cause", CAUSE), ("status", STATUS), ("behaviour", BEHAVE),
                           ("environment", ENV), ("element", ELEMENT)) if rx.search(s)]
    if HEDGE.search(s) and cls:
        cls.append("hedge")
    return cls


def elements(s):
    return [m.group(0).strip().strip("`") for m in ELEMENT.finditer(s)]


# ---------------------------------------------------------------- receipt store and minting
def _append(path, row):
    with open(path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def load(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def prose_of(page):
    """Page body minus fenced and indented blocks: only prose can govern a claim."""
    keep, fenced = [], False
    for ln in str(page or "").split("\n"):
        if FENCE.match(ln):
            fenced = not fenced
        elif not fenced and not INDENT.match(ln):
            keep.append(ln)
    return "\n".join(keep)


def mint_docs(path, ticket, url, quote, page_body, claims, now, extra=None):
    """Refuse unless the quote sits verbatim in the PROSE of a page the harness fetched from an allowed host.
    `extra` is merged into the row: the desk records where the captured body came from (page_origin)."""
    host = re.sub(r"^https?://", "", str(url or "").strip(), flags=re.I).split("/")[0].lower()
    if host not in POLICY["docs_hosts"]:
        return {"ok": False, "why": "docs-host-not-official"}
    if re.match(r"\s*(for example|example|sample|e\.g\.)", norm(quote)) or len(quote.split()) < 4:
        return {"ok": False, "why": "docs-example-or-fragment"}
    if norm(quote) not in norm(prose_of(page_body)):
        return {"ok": False, "why": "quote-not-in-prose"}
    row = {"id": "rcpt:docs-" + hashlib.sha256((url + quote).encode()).hexdigest()[:8], "kind": "docs",
           "ticket": str(ticket), "minted": now.isoformat(), "source": url, "quote": quote,
           "body_sha": hashlib.sha256(page_body.encode()).hexdigest(), "bound": list(claims)}
    row.update({k: v for k, v in (extra or {}).items() if v is not None})
    _append(path, row)
    return {"ok": True, "id": row["id"]}


def mint_test(path, ticket, did, output, origin, claims, now, snippet=None):
    """Recorded output of a real run. A model's description of the output is not the output."""
    if origin not in POLICY["good_origins"]:
        return {"ok": False, "why": "origin-not-captured"}
    if not str(output or "").strip() or not str(did or "").strip():
        return {"ok": False, "why": "empty-record"}
    row = {"id": "rcpt:test-" + hashlib.sha256((did + output).encode()).hexdigest()[:8], "kind": "test",
           "ticket": str(ticket), "minted": now.isoformat(), "did": did, "quote": output, "origin": origin,
           "snippet": snippet, "bound": list(claims)}
    _append(path, row)
    return {"ok": True, "id": row["id"]}


def mint_material(path, ticket, quote, inbound, claims, now):
    """The customer's own words or file, found in the recorded inbound. About THEIR system only."""
    if norm(plain(quote)) not in norm(plain(inbound)):
        return {"ok": False, "why": "quote-not-in-material"}
    row = {"id": "rcpt:material-" + hashlib.sha256(quote.encode()).hexdigest()[:8], "kind": "material",
           "ticket": str(ticket), "minted": now.isoformat(), "quote": quote, "bound": list(claims)}
    _append(path, row)
    return {"ok": True, "id": row["id"]}


# ---------------------------------------------------------------- copy-back and escalation
def words(s):
    return re.findall(r"[a-z0-9']+", norm(plain(s)))


def echo_share(draft, inbound, n=None):
    """Share of the draft's words inside a run of n consecutive words copied from the customer's inbound. 0.0 when
    there is no inbound to compare, which is "cannot tell", not "clean"."""
    n = n or POLICY["echo_ngram"]
    dw, iw = words(draft), words(inbound)
    if len(dw) < n or len(iw) < n:
        return 0.0
    grams = {tuple(iw[i:i + n]) for i in range(len(iw) - n + 1)}
    hit = [False] * len(dw)
    for i in range(len(dw) - n + 1):
        if tuple(dw[i:i + n]) in grams:
            hit[i:i + n] = [True] * n
    return round(sum(hit) / float(len(dw)), 3)


# A reply that escalates, or says it did, needs a recorded mismatch: the docs say X, the product did Y. The fleet's
# claim-check ESC_TEXT, plus the operator's own escalation sentence ("I've passed along your request to the WizOS
# engineering team.", the wording the drafter is told to use), which ESC_TEXT does not match.
ESCALATION = re.compile(r"\bpass(?:ed|ing)?\s+(?:along|on)\s+(?:your|the|this)\s+(?:request|question|report|case)\s+to\b[^.\n]{0,40}\bengineering\b|\bescalat\w*|\b(?:take|taking|took|raise|raising|raised|bring|bringing|pass|passing|send|sending|"
                        r"flag|flagging|log|logging|file|filing)\s+(?:it|this|that|them)\s+(?:to|with|up with)\s+"
                        r"(?:our\s+|the\s+)?(?:engineering|product|dev|development|r&d)\b|\b(?:filed|opened|raised|logged)\s+"
                        r"(?:a\s+|an\s+)?(?:bug|defect|engineering (?:ticket|case))|\bengineering (?:team )?(?:is|are|will|to) "
                        r"(?:look|investigat|review|check)", re.I)


def mint_mismatch(path, ticket, documented, observed, summary, now):
    """The basis for an escalation: a fresh docs receipt (what the product should do) and a fresh material or test
    receipt (what it did), both on this ticket, and one line saying how they differ."""
    mine = {r["id"]: r for r in load(path) if r.get("ticket") == str(ticket)}
    d, o = mine.get(documented), mine.get(observed)
    if not d or d.get("kind") != "docs":
        return {"ok": False, "why": "documented-not-a-docs-receipt"}
    if not o or o.get("kind") not in ("material", "test"):
        return {"ok": False, "why": "observed-not-a-material-or-test-receipt"}
    if not (_fresh(d, now) and _fresh(o, now)):
        return {"ok": False, "why": "receipt-stale"}
    if len(str(summary or "").split()) < 4:
        return {"ok": False, "why": "summary-missing"}
    row = {"id": "rcpt:mismatch-" + hashlib.sha256((documented + observed).encode()).hexdigest()[:8], "kind": "mismatch",
           "ticket": str(ticket), "minted": now.isoformat(), "documented": documented, "observed": observed,
           "quote": str(summary), "bound": []}
    _append(path, row)
    return {"ok": True, "id": row["id"]}


def escalation(draft, ticket, path, now):
    """None when the draft does not escalate; else the sentence and the mismatch ids that still stand behind it."""
    said = [u for k, u in units(draft) if k == "sentence" and ESCALATION.search(u)]
    if not said:
        return None
    mine = {r["id"]: r for r in load(path) if r.get("ticket") == str(ticket)}
    basis = [r["id"] for r in mine.values() if r["kind"] == "mismatch" and _fresh(r, now)
             and all(mine.get(r[k]) and _fresh(mine[r[k]], now) for k in ("documented", "observed"))]
    return {"sentence": said[0], "mismatch": basis}


# ---------------------------------------------------------------- the check
def _fresh(r, now):
    return now - datetime.fromisoformat(r["minted"]) <= timedelta(hours=POLICY["max_age_hours"])


def _problems(c, text, bound, inbound):
    """Why the receipts bound to one claim do not carry it. Empty list means verified."""
    kinds = {r["kind"] for r in bound}
    ev = "\n".join(r["quote"] for r in bound)
    cw, shared = content_words(text), content_words(text) & content_words(ev)
    p = []
    if not shared:
        return ["receipt-unrelated"]
    leg = [r for r in bound if r["kind"] in ("docs", "test")]
    if "instruction" in c and not leg:
        p.append("instruction-needs-docs-or-test")
    if "cause" in c and not (leg and "material" in kinds):
        p.append("cause-needs-product-leg-and-material")
    if "behaviour" in c and not leg and "cause" not in c:
        p.append("behaviour-needs-docs-or-test")
    if len(cw) >= POLICY["weak_min_words"] and len(shared) / len(cw) < POLICY["support_share"] and leg:
        p.append("quote-does-not-support")
    frag = " ".join(f for r in bound for f in r["bound"])
    loose = cw - content_words(ev) - content_words(frag)
    if len(loose) >= POLICY["unbound_words"] and len(loose) / len(cw) >= POLICY["unbound_share"]:
        p.append("partial-binding")
    if any(squash(e) not in squash(ev) for e in elements(text)):
        p.append("element-not-in-evidence")
    if CONDITION.search(text) and not any(CONDITION.search(r["quote"]) for r in leg if r["kind"] == "docs"):
        p.append("conditional-paraphrase")
    if NEGATION.search(text) and not any(NEGATION.search(r["quote"]) for r in leg):
        p.append("polarity-not-in-quote")
    return p


def check(draft, ticket, path, now, inbound=""):
    mine = [r for r in load(path) if r["ticket"] == str(ticket)]
    results, in_cw = [], content_words(inbound)
    for kind, u in units(draft):
        if kind == "snippet":
            tested = [r for r in mine if _fresh(r, now) and r["kind"] == "test"
                      and squash(r.get("snippet") or "") == squash(u)]
            results.append({"text": u[:80], "classes": ["snippet"], "problems": [] if tested else ["snippet-untested"]})
            continue
        c = classify(u)
        if UNCONFIRMED.search(u):   # hedging is not a way out: verified, or absent
            results.append({"text": u, "classes": c, "problems": ["unconfirmed-wording"]})
            continue
        if not c:
            continue
        cand = [r for r in mine if any(norm(f) and norm(f) in norm(u) for f in r["bound"])]
        bound = [r for r in cand if _fresh(r, now)]
        res = {"text": u, "classes": c, "receipts": [r["id"] for r in bound], "problems": []}
        cw = content_words(u)
        if len(cw) >= POLICY["weak_min_words"] and len(cw & in_cw) / len(cw) >= POLICY["echo_share"] \
                and not [r for r in bound if r["kind"] in ("docs", "test")]:
            res.update(echo=True, problems=[])      # restating the customer: no credit, no receipt needed
        elif not bound:
            res["problems"] = ["receipt-stale" if cand else "uncovered"]
        else:
            res["problems"] = _problems(c, u, bound, inbound)
        if "hedge" in c and not res["problems"]:
            res["problems"] = ["hedged-claim"]       # verified means say it plainly
        results.append(res)
    credited = [r for r in results if r.get("receipts") and not r["problems"] and not r.get("echo")]
    hits = sorted({p for r in results for p in r["problems"]})
    if any(r.get("echo") for r in results) and not credited:
        hits.append("echo-only")
    return {"verdict": "hold" if hits else "ok", "hits": hits, "claims": results, "credited": len(credited)}
