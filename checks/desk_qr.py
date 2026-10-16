"""A filed draft as QR codes the operator's screen can carry byte for byte.

2026-10-16 (#147802, F-1096): the usual bar's step S2 needs the draft's exact bytes on box j1, where go-ledger matches them to the
desk md5. No file channel runs from the workstation to box j1; the screen already does (J1 reads it from snapshots). Each code
carries `ADQ1 <md5> <i>/<n> <base64 slice>`; the reader joins the slices in order, decodes the text and checks its md5 against the
one in every code, so a misread is refused, never mistaken for the draft. Base64 keeps the payload ASCII: a decoder's charset
guess cannot change a byte.
"""
import base64, hashlib, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import qrcode  # noqa: E402
from qrcode.constants import ERROR_CORRECT_M  # noqa: E402

PREFIX = "ADQ1"
QUIET = 4
# byte-mode capacity at ECC level M, versions 1 to 20 (ISO/IEC 18004 table 7)
# the header alone (prefix, md5, i/n) is 47 bytes: version 5 is the smallest with room for a slice
MIN_VERSION = 5
CAP_M = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666]


def version_for(width):
    """The largest version whose code and quiet zone fit `width` terminal columns (one column per module)."""
    v = (int(width) - 2 * QUIET - 17) // 4
    return max(MIN_VERSION, min(20, v))


def payloads(md5, text, version):
    data = base64.b64encode(text.encode("utf-8")).decode("ascii")
    head = len("%s %s 99/99 " % (PREFIX, md5))
    room = CAP_M[version - 1] - head
    room -= room % 4                                   # whole base64 quads per code
    if room < 4:
        raise ValueError("version %d has no room for a slice" % version)
    parts = [data[i:i + room] for i in range(0, len(data), room)] or [""]
    n = len(parts)
    return ["%s %s %d/%d %s" % (PREFIX, md5, i + 1, n, p) for i, p in enumerate(parts)]


def matrix(payload, version):
    q = qrcode.QRCode(version=version, error_correction=ERROR_CORRECT_M, border=QUIET)
    q.add_data(payload, optimize=0)
    q.make(fit=False)
    return q.get_matrix()


def half_blocks(m):
    """Two module rows per text line: light modules are drawn (the glyph), dark ones are the terminal's background,
    so on a dark terminal the code reads dark-on-light as a scanner expects. A light terminal shows it inverted;
    the reader tries both."""
    rows = [[not c for c in r] for r in m]            # True = light
    if len(rows) % 2:
        rows.append([True] * len(rows[0]))
    glyph = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return ["".join(glyph[(a, b)] for a, b in zip(rows[y], rows[y + 1])) for y in range(0, len(rows), 2)]


def codes(md5, text, width=72):
    if hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest() != md5:
        return {"ok": False, "reason": "text is not md5 %s" % md5}
    v = version_for(width)
    out = []
    for p in payloads(md5, text, v):
        out.append({"payload": p, "lines": half_blocks(matrix(p, v))})
    return {"ok": True, "md5": md5, "version": v, "n": len(out), "chars": len(text), "codes": out}


def join(found):
    """found: payload strings read from any number of frames, in any order, repeats allowed. Returns the text once
    every slice of one md5 is present and the joined text hashes to it."""
    by = {}
    for s in found:
        bits = (s or "").strip().split(" ")
        if len(bits) < 3 or bits[0] != PREFIX or "/" not in bits[2]:
            continue
        md5, (i, n) = bits[1], bits[2].split("/", 1)
        try:
            i, n = int(i), int(n)
        except ValueError:
            continue
        by.setdefault((md5, n), {})[i] = bits[3] if len(bits) > 3 else ""
    if not by:
        return {"ok": False, "reason": "no desk QR code in what was read", "have": [], "missing": []}
    (md5, n), parts = max(by.items(), key=lambda kv: len(kv[1]))
    missing = [i for i in range(1, n + 1) if i not in parts]
    if missing:
        return {"ok": False, "reason": "codes missing", "md5": md5, "n": n, "have": sorted(parts), "missing": missing}
    try:
        text = base64.b64decode("".join(parts[i] for i in range(1, n + 1)), validate=True).decode("utf-8")
    except Exception as e:
        return {"ok": False, "reason": "slices do not decode: %s" % e, "md5": md5, "n": n, "have": sorted(parts), "missing": []}
    got = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    if got != md5:
        return {"ok": False, "reason": "joined text is md5 %s, not %s" % (got, md5), "md5": md5, "n": n, "have": sorted(parts), "missing": []}
    return {"ok": True, "md5": md5, "n": n, "text": text}
