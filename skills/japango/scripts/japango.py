"""japango: number sentences, merge finder runs, assemble the revised text.

Standard library only (Python 3.8+).

  ids SRC WORKDIR
      Split the body into paragraphs and sentences. Writes, inside WORKDIR,
      <stem>.units.jsonl (one unit per sentence or line), <stem>.annotated.txt
      (what the finder reads, one "[p2-s3] sentence" per line) and
      <stem>.orig.md (the units rendered back). Exits 1 when the rendered text
      differs from the source.

  merge UNITS OUT MODE FINDINGS [FINDINGS ...]
      MODE is "agree" (an edit needs every run to propose an edit for that ID;
      the default for two runs) or "union" (any run's edit counts). Questions
      are always the union. Also adds a mechanical finding for bold that wraps
      a whole sentence or appears twice in one sentence.

  assemble UNITS MERGED OUT_MD LOG_JSON
      Apply the merged findings without a separate rewriting step:
      delete -> the sentence goes, rewrite -> the finder's sentence replaces it,
      ask -> the sentence stays and gets a 〔確認n〕 mark. A rewritten sentence that
      brings a cause, limit or time word, or a number, that the whole original
      never had is put back to the original. Questions are listed at the end.
"""
import argparse
import json
import re
import sys
from pathlib import Path

# a sentence ends at 。！？ plus the closing marks that belong to it (bold, brackets)
SENT = re.compile(r"[^。！？]*[。！？](?:\*\*|[」』）)])*|[^。！？]+$")
BLOCK_START = re.compile(r"(#{1,6} |[-*+] |\d+[.)] |> )")
FENCE = re.compile(r"\s*(```|~~~)")

ROUTES = ("delete", "rewrite", "ask", "keep")
EDIT_ROUTES = ("rewrite", "delete")

# words that state a cause or a limit, and words that bound a time
ADDED_MARKERS = ["だけ", "のみ", "紛れ", "ため", "により", "によって", "原因", "せいで"]
TIME_MARKERS = ["まで", "以降", "以前", "までに"]
NUM = re.compile(r"\d+(?:\.\d+)?")
BOLD = re.compile(r"\*\*(.+?)\*\*")

# questions about deadlines or owners of future work are not what the text is missing;
# keep questions about who decided, why, and what caused it
ASK_KEEP = re.compile(r"誰|なぜ|理由|根拠|原因|どの|何を|出典")
ASK_DROP = re.compile(r"いつまで|期限|期日|予定")


# ---------- files ----------

def write_text(path, s):
    Path(path).write_text(s, encoding="utf-8", newline="\n")


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def read_jsonl(path, warn=None):
    """Rows of a JSON-lines file. Lines that are not JSON objects are skipped
    (and counted in warn, a list, when given)."""
    rows = []
    for n, line in enumerate(read_text(path).splitlines(), 1):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if warn is not None:
                warn.append(f"{path}:{n}: not JSON")
    return rows


def write_jsonl(path, rows):
    write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# ---------- ids ----------

def split_front(text):
    """(front matter including the blank lines after it, body)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            cut = end + 5
            rest = text[cut:]
            body = rest.lstrip("\n")
            return text[:cut] + rest[: len(rest) - len(body)], body
    body = text.lstrip("\n")
    return text[: len(text) - len(body)], body


def blocks_of(body):
    """[(gap, lines, is_code)]: paragraphs split at blank lines, fenced code kept whole.
    gap is the exact text that separated the block from the previous one."""
    out, cur, gap, fence = [], [], [], False
    for line in body.rstrip("\n").split("\n"):
        if fence:
            cur.append(line)
            if FENCE.match(line):
                out.append(("\n".join([""] + gap + [""]), cur, True))
                cur, gap, fence = [], [], False
            continue
        if FENCE.match(line):
            if cur:
                out.append(("\n".join([""] + gap + [""]), cur, False))
                gap = []
            cur, fence = [line], True
            continue
        if not line.strip():
            if cur:
                out.append(("\n".join([""] + gap + [""]), cur, False))
                cur, gap = [], []
            gap.append(line)
            continue
        cur.append(line)
    if cur:
        out.append(("\n".join([""] + gap + [""]), cur, fence))
    return out


def is_raw(lines):
    """Tables and HTML are not split; the finder does not see them."""
    return all(l.lstrip().startswith("|") for l in lines) or lines[0].lstrip().startswith("<")


def sentences(para):
    """Split one paragraph into sentences, keeping leading spaces. A ** right after
    the end mark opens the next bold when the sentence has an odd number of **."""
    out = [x for x in SENT.findall(para) if x]
    for k in range(len(out) - 1):
        if out[k].endswith("**") and out[k].count("**") % 2 == 1:
            out[k], out[k + 1] = out[k][:-2], "**" + out[k + 1]
    return out


def units(text):
    """Units of the body. Each has id, text, para, line (one unit per line)
    and raw (kept as is, not shown to the finder)."""
    out = []
    for pi, (gap, lines, code) in enumerate(blocks_of(split_front(text)[1]), 1):
        base = {"para": pi, "gap": gap}
        if code or is_raw(lines):
            out.append(dict(base, id=f"p{pi}-s1", text="\n".join(lines), line=True, raw=True))
            continue
        if len(lines) > 1 or BLOCK_START.match(lines[0]):
            for si, l in enumerate(lines, 1):
                out.append(dict(base, id=f"p{pi}-s{si}", text=l, line=True))
            continue
        # keep each sentence's leading spaces so rendering restores the original
        for si, s in enumerate(sentences(lines[0]), 1):
            out.append(dict(base, id=f"p{pi}-s{si}", text=s, line=False))
    return out


def render(us):
    """Join units back into text. Empty units (deleted) drop out; a paragraph
    whose units are all empty drops out with its gap."""
    paras = []
    for u in us:
        if not paras or paras[-1][0] != u["para"]:
            paras.append((u["para"], u.get("gap", "\n\n"), u.get("line", False), []))
        if u["text"]:
            paras[-1][3].append(u["text"])
    out = ""
    for _, gap, is_line, texts in paras:
        if not texts:
            continue
        block = ("\n" if is_line else "").join(texts)
        out += (gap if out else "") + block
    return out + "\n"


def expected_body(text):
    return split_front(text)[1].rstrip("\n") + "\n"


def cmd_ids(src, workdir):
    text = read_text(src)
    us = units(text)
    out = Path(workdir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(src).stem
    front = split_front(text)[0]
    write_jsonl(out / f"{stem}.units.jsonl", [{"front": front}] + us if front else us)
    write_text(out / f"{stem}.annotated.txt",
               "".join(f"[{u['id']}] {u['text'].strip()}\n" for u in us if not u.get("raw") and u["text"].strip()))
    rendered = render(us)
    write_text(out / f"{stem}.orig.md", front + rendered)
    ok = rendered == expected_body(text)
    print(json.dumps({"units": len(us), "round_trip": "ok" if ok else "DIFF",
                      "annotated": str(out / f"{stem}.annotated.txt")}, ensure_ascii=False))
    return 0 if ok else 1


def load_units(path):
    rows = read_jsonl(path)
    front = next((r["front"] for r in rows if "front" in r and "id" not in r), "")
    return front, [r for r in rows if "id" in r]


# ---------- merge ----------

def clean_ask(q):
    """〔誰が判断したか〕 -> 誰が判断したか (only when the whole text is one bracket)."""
    q = (q or "").strip()
    if q.startswith("〔") and q.endswith("〕") and q.count("〔") == 1:
        return q[1:-1]
    return q


def useful_ask(q):
    return bool(q) and not (ASK_DROP.search(q) and not ASK_KEEP.search(q))


def merge_findings(rows):
    """Collapse several findings for one ID into one row.

    keep wins (the less destructive side when runs disagree). Otherwise an edit
    (rewrite, then delete) wins over ask, so a run that only asked cannot keep a
    sentence another run fixed; its question still goes to the confirmation list.
    Questions come from the earliest run that asked, so two runs do not stack
    the same question twice.
    """
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    merged = []
    for i, rs in by_id.items():
        routes = {r["route"] for r in rs}
        ask_rows = [r for r in rs if r["route"] == "ask" and r.get("fix")]
        first_src = min((r.get("src", 0) for r in ask_rows), default=None)
        asks = "".join(r["fix"] for r in ask_rows if r.get("src", 0) == first_src)
        if "keep" in routes:
            merged.append({"id": i, "route": "keep", "fix": ""})
        elif "rewrite" in routes:
            first = next(r for r in rs if r["route"] == "rewrite")
            merged.append({"id": i, "route": "rewrite", "fix": first.get("fix", ""), "ask": asks,
                           "pattern": first.get("pattern", "")})
        elif "delete" in routes:
            first = next(r for r in rs if r["route"] == "delete")
            merged.append({"id": i, "route": "delete", "fix": "", "ask": asks,
                           "pattern": first.get("pattern", "")})
        else:
            merged.append({"id": i, "route": "ask", "fix": asks})
    return merged


def merge_findings_agree(rows, runs):
    """Edits only where every run proposed an edit for the ID; questions are the union."""
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    kept = []
    for rs in by_id.values():
        editors = {r.get("src", 0) for r in rs if r["route"] in EDIT_ROUTES}
        if any(r["route"] == "keep" for r in rs) or len(editors) < runs:
            rs = [r for r in rs if r["route"] == "ask"]
        kept += rs
    return merge_findings(kept)


def unbold(t):
    """Drop ** but keep a space between kana/kanji and a digit, as many documents do."""
    def keep_space(m):
        before = t[m.start() - 1] if m.start() else ""
        after = t[m.end()] if m.end() < len(t) else ""
        inner = m.group(1)
        lead = " " if before.isalpha() and not before.isascii() and inner[:1].isdigit() else ""
        tail = " " if after.isalpha() and not after.isascii() and inner[-1:].isdigit() else ""
        return lead + inner + tail
    return BOLD.sub(keep_space, t)


def bold_findings(us):
    """Bold that wraps a whole sentence, or more than one bold span in a sentence."""
    out = []
    for u in us:
        t = u["text"].strip()
        if u.get("raw") or t.startswith("#"):
            continue
        spans = BOLD.findall(t)
        whole = t.startswith("**") and t.rstrip("。").endswith("**") and len(spans) == 1
        if whole or len(spans) > 1:
            out.append({"id": u["id"], "route": "rewrite", "fix": unbold(t), "pattern": "太字"})
    return out


def merge(us, runs, mode="agree"):
    """runs: one list of findings per finder run. Returns (merged, warnings)."""
    ids = {u["id"] for u in us if not u.get("raw")}
    warnings, rows = [], []
    for n, run in enumerate(runs):
        seen = set()
        for r in run:
            i, route = r.get("id"), r.get("route")
            if i not in ids or route not in ROUTES:
                warnings.append(f"run {n + 1}: skipped {i!r} (route {route!r})")
                continue
            if (i, route) in seen:
                continue
            seen.add((i, route))
            if route == "ask" and not useful_ask(r.get("fix")):
                continue
            rows.append(dict(r, src=n))
    merged = merge_findings_agree(rows, len(runs)) if mode == "agree" else merge_findings(rows)
    # the bold check only speaks for sentences no finder run mentioned
    mentioned = {r["id"] for r in rows}
    merged += [b for b in bold_findings(us) if b["id"] not in mentioned]
    order = {u["id"]: k for k, u in enumerate(us)}
    merged.sort(key=lambda r: order[r["id"]])
    return merged, warnings


def cmd_merge(units_path, out_path, mode, finding_paths):
    _, us = load_units(units_path)
    warn = []
    runs = [read_jsonl(p, warn) for p in finding_paths]
    merged, w = merge(us, runs, mode)
    write_jsonl(out_path, merged)
    counts = {k: sum(1 for r in merged if r["route"] == k) for k in ROUTES}
    print(json.dumps({"runs": len(runs), "mode": mode, "routes": counts, "warnings": warn + w},
                     ensure_ascii=False))
    return 0


# ---------- assemble ----------

def added_words(new, orig_doc):
    """Cause, limit and time words, and numbers, in new that the whole original never had."""
    hits = [m for m in ADDED_MARKERS + TIME_MARKERS if m in new and m not in orig_doc]
    orig_nums = set(NUM.findall(orig_doc))
    hits += [n for n in NUM.findall(new) if n not in orig_nums]
    return hits


def assemble(us, merged):
    """Returns (body, log). us is changed in place."""
    by_id = {u["id"]: u for u in us}
    orig_doc = "".join(u["text"] for u in us)
    before = {u["id"]: u["text"] for u in us}
    changes, reverted, confirm, skipped = [], [], [], []
    for f in merged:
        u = by_id.get(f.get("id"))
        if not u or u.get("raw"):
            skipped.append(f.get("id"))
            continue
        if f["route"] == "keep":
            continue
        old = u["text"]
        lead = old[: len(old) - len(old.lstrip())]
        prefix = BLOCK_START.match(old.lstrip())
        prefix = prefix.group(1) if prefix else ""
        if f["route"] == "delete":
            new = ""
            if f.get("ask"):
                confirm.append({"id": f["id"], "ask": f["ask"]})
        elif f["route"] == "ask":
            new = old.strip()
            confirm.append({"id": f["id"], "ask": f["fix"]})
        else:
            new = (f.get("fix") or "").strip()
            hits = added_words(new, orig_doc)
            if hits or not new:
                reverted.append({"id": f["id"], "proposed": new, "words": hits or ["空の言い換え"]})
                new = old.strip()
            if f.get("ask"):
                confirm.append({"id": f["id"], "ask": f["ask"]})
        if new and prefix and not new.startswith(prefix.strip()):
            new = prefix + new
        u["text"] = (lead + new) if new else ""
        if u["text"] != old:
            changes.append({"id": f["id"], "route": f["route"], "pattern": f.get("pattern", ""),
                            "before": old.strip(), "after": u["text"].strip()})

    # The body carries only a short mark after the sentence; the questions go to the
    # confirmation list. A question about a deleted sentence is marked on the nearest
    # earlier sentence that remains, numbers follow the order in the body, and the
    # same question asked twice gets one number.
    order = {u["id"]: k for k, u in enumerate(us)}

    def markable(x):
        t = x["text"].strip()
        return t and not x.get("raw") and not t.startswith("#")

    def anchor(i):
        for j in range(order[i], -1, -1):
            if markable(us[j]):
                return us[j]["id"]
        return next((x["id"] for x in us[order[i]:] if markable(x)), i)

    for c in confirm:
        c["at"] = anchor(c["id"])
    confirm.sort(key=lambda c: (order[c["at"]], order[c["id"]]))
    unique, seen = [], set()
    for c in confirm:
        if c["ask"] in seen:
            continue
        seen.add(c["ask"])
        unique.append(c)
    for n, c in enumerate(unique, 1):
        c["no"] = n
        by_id[c["at"]]["text"] += f"〔確認{n}〕"
    body = render(us)
    if unique:
        body += "\n---\n\n## 確認欄\n\n" + "".join(f"- 〔確認{c['no']}〕{clean_ask(c['ask'])}\n" for c in unique)
    log = {"changes": changes, "reverted": reverted, "confirm": unique,
           "skipped": [s for s in skipped if s], "unchanged_ids": len(before) - len(changes)}
    return body, log


def cmd_assemble(units_path, merged_path, out_path, log_path):
    front, us = load_units(units_path)
    body, log = assemble(us, read_jsonl(merged_path))
    write_text(out_path, front + body)
    write_text(log_path, json.dumps(log, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"changed": len(log["changes"]), "reverted": len(log["reverted"]),
                      "confirm": len(log["confirm"]), "out": str(out_path)}, ensure_ascii=False))
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="japango.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ids", help="number sentences")
    a.add_argument("src")
    a.add_argument("workdir")
    m = sub.add_parser("merge", help="merge finder runs")
    m.add_argument("units")
    m.add_argument("out")
    m.add_argument("mode", choices=["agree", "union"])
    m.add_argument("findings", nargs="+")
    s = sub.add_parser("assemble", help="build the revised text")
    s.add_argument("units")
    s.add_argument("merged")
    s.add_argument("out_md")
    s.add_argument("log_json")
    args = p.parse_args(argv)
    if args.cmd == "ids":
        return cmd_ids(args.src, args.workdir)
    if args.cmd == "merge":
        return cmd_merge(args.units, args.out, args.mode, args.findings)
    return cmd_assemble(args.units, args.merged, args.out_md, args.log_json)


if __name__ == "__main__":
    sys.exit(main())
