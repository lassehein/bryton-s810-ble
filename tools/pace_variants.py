"""Make small variants of a captured Pace Pilot report to find out what the device validates.

  python3 tools/pace_variants.py report.bin OUTDIR

Edits are made on the raw text, so every variant has the same size and layout as the original:
A: segment 1 target_power_w_low changed by one watt (data only)
D: tactical note of segment 1 replaced by a text of the same byte length (text only)
B: screen fields "Segment_Distance" and "Grade" swap position AND priority_score (consistent swap)
The output is derived from your own capture: keep it out of public repositories.
"""
import json
import re
import sys
from pathlib import Path


def variants(raw):
    text = raw.decode("utf-8")
    out = {}
    m = re.search(r'"target_power_w_low" : (\d+)', text)
    out["A"] = text[:m.start(1)] + str(int(m.group(1)) + 1) + text[m.end(1):]  # 120 -> 121, same length
    m = re.search(r'"tactical_note" : "([^"]*)"', text)
    old = m.group(1)
    new = ("Ruhig bleiben " * 10).encode()[:len(old.encode())].decode("ascii").ljust(len(old.encode()), "x")
    out["D"] = text[:m.start(1)] + new + text[m.end(1):]
    blocks = {name: re.search(r'\{[^{}]*"data_field" : "%s"[^{}]*\}' % name, text) for name in ("Segment_Distance", "Grade")}
    b1, b2 = blocks["Segment_Distance"], blocks["Grade"]
    pos = lambda b: re.search(r'"position" : "([^"]*)"', b.group(0)).group(1)
    prio = lambda b: re.search(r'"priority_score" : (\d+)', b.group(0)).group(1)
    p1, p2, s1, s2 = pos(b1), pos(b2), prio(b1), prio(b2)

    def swap(block, new_pos, new_prio):
        t = re.sub(r'("position" : )"[^"]*"', lambda mm: f'{mm.group(1)}"{new_pos}"', block.group(0))
        return re.sub(r'("priority_score" : )\d+', lambda mm: f"{mm.group(1)}{new_prio}", t)

    parts = sorted([(b1.start(), b1.end(), swap(b1, p2, s2)), (b2.start(), b2.end(), swap(b2, p1, s1))])
    t, last = "", 0
    for start, end, repl in parts:
        t += text[last:start] + repl
        last = end
    out["B"] = t + text[last:]
    result = {}
    for key, value in out.items():
        data = value.encode("utf-8")
        assert len(data) == len(raw) and data != raw, key
        json.loads(data)
        result[key] = data
    return result


if __name__ == "__main__":
    raw = open(sys.argv[1], "rb").read()
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for key, data in variants(raw).items():
        (out / f"report_{key}.json").write_bytes(data)
        print(key, len(data), "bytes, original", len(raw), "bytes differing:", sum(a != b for a, b in zip(data, raw)))
