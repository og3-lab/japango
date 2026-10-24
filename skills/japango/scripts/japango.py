"""japango: number sentences, merge finder runs, assemble the revised text.

Standard library only (Python 3.8+).

  ids SRC WORKDIR
      Split the body into paragraphs and sentences. Writes, inside WORKDIR,
      <stem>.units.jsonl (one unit per sentence or line), <stem>.annotated.txt
      (what the finder reads, one "[p2-s3] sentence" per line) and
      <stem>.orig.md (the units rendered back). The first row of the units file
      records the source path, so assemble can write next to it. Exits 1 when
      the rendered text differs from the source.

  merge UNITS OUT MODE FINDINGS [FINDINGS ...]
      MODE is "agree" (an edit needs every run to propose an edit for that ID;
      the default for two runs) or "union" (any run's edit counts). A keep from
      any run is kept as a keep row. Routes are delete, rewrite and keep; a
      finding with another route (the old ask) is skipped with a warning.
      A rewrite may carry "reading": how the finder read a sentence that could
      be read more than one way. It goes with the rewrite that is taken. Also
      adds a mechanical finding for bold that wraps a whole sentence or appears
      twice in one sentence.
      A rewrite for types 21, 21d and 21e names the word it aligns to in "align".
      When runs rewrite one ID toward different words, the first run's rewrite
      is used and its reading names the words.
      In union mode, a rewrite only one run proposed gets "one_run".
      A rewrite may join sentences: "join" lists the IDs it replaces, the first
      being "id", all in a row in one prose paragraph (not headings or list
      items). A join is taken only when no run keeps a sentence in it; in agree
      mode every other run must also edit a sentence in it. When joins overlap,
      the first run's wins ("join span differs"); in union mode a join drops
      the other edits inside it (with a warning).

  assemble UNITS MERGED LOG_JSON [--out OUT_MD]
      Write the revised text to <source stem>.japango.md next to the source
      (<stem>.japango-2.md, -3 ... when the name is taken; the source itself is
      never written). --out writes elsewhere instead.
      Apply the merged findings without a separate rewriting step:
      delete -> the sentence goes, rewrite -> the finder's sentence replaces it.
      A rewritten sentence that brings a cause, limit or time word, or a number,
      that the whole original never had is put back to the original. keep rows
      are logged in changes as 残した. A join puts its sentence where the first
      one was and empties the rest; when it is put back, every sentence in it is.
      Nothing asks the writer for facts. The list at the end
      (## 読みを選んだ箇所・見直す箇所) names the readings of the rewrites that
      were used ("readings"), the rewrites only one run proposed ("one_run"),
      and three things that are only reported, never fixed: a word that appears
      only in a deleted sentence while a later sentence still points at it
      (両課, この方式, 同社 ...) goes to "dangling", a prose paragraph left with
      one sentence after deletions goes to "orphans", and a sentence deleted as
      a restatement (型 6) that held a word found nowhere else in the body goes
      to "lost". No list is written when there is nothing to list.
      A second round (ids on an assembled text): its list is one raw unit, left
      out of the added-fact check, and the new lines go at its end.

  extract PAGE.html -o OUT_MD
      Take the text out of an HTML page as Markdown for ids: headings as #, list
      items, paragraphs, dt and dd; head, script, style and svg dropped. Buttons,
      code and pictures become 〔ボタン: text → href〕, 〔コード: ...〕 and
      〔画像: alt〕 lines, which ids keeps as is (raw).
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

ROUTES = ("delete", "rewrite", "keep")
# types whose rewrite aligns a word with the rest of the text (呼び名・人の呼び名・動詞)
ALIGN_TYPES = re.compile(r"型\s*21[de]?(?![0-9a-z])")
# runs that align one sentence toward different words: the first run's rewrite is
# used, and this reading says so
ALIGN_READING = "そろえる語が回で分かれた（{words}）。先の回の言い方で直した"
EDIT_ROUTES = ("rewrite", "delete")

# words that state a cause or a limit, and words that bound a time
ADDED_MARKERS = ["だけ", "のみ", "紛れ", "ため", "により", "によって", "原因", "せいで"]
TIME_MARKERS = ["まで", "以降", "以前", "までに"]
NUM = re.compile(r"\d+(?:\.\d+)?")
BOLD = re.compile(r"\*\*(.+?)\*\*")

# nouns for the dangling-reference check: runs of 2+ kanji or katakana
NOUN = re.compile(r"[\u4E00-\u9FFF々〆ヶァ-ヴー]{2,}")
# a pointing word and the head noun right after it (両課, この方式, 同社, 前述の手順)
POINTER = re.compile(r"(?:(?<![\u4E00-\u9FFF々])(?:両|同|当該|上記の?|前述の?|先の)|この|その|これらの|それらの)"
                     r"([\u4E00-\u9FFF々〆ヶァ-ヴー]{1,6})")

# the list assemble writes at the end: "---", a blank line, the heading, a blank line,
# then only lines of the forms reading_line, one_run_line, dangling_line, lost_line
# and orphan_line write. None of them asks the writer for facts
CONFIRM_HEAD = "---\n\n## 読みを選んだ箇所・見直す箇所\n\n"
CONFIRM_ITEM = re.compile(r"(?:- 読みを選んだ: |- 片方の回だけが直した文: |- 削った文が指していた語が残っていません: "
                          r"|- 1 文だけ残った段落: |- 言い直しとして削った文にしか無い語: )")
# the lines extract writes for a button, code and a picture
MARK_LINE = re.compile(r"〔(?:ボタン|コード|画像): ")
# a deletion as a restatement (型 6) says the sentence adds nothing; a word found
# nowhere else in the body says otherwise
RESTATE = re.compile(r"^\s*型\s*6(?![0-9a-z])")
# a line break inside a reading or a rewritten sentence: the list writes it on one line
LINE_BREAK_IN = re.compile(r"[ \t]*\n[ \t]*")


def one_line(s):
    return LINE_BREAK_IN.sub(" ", s)


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
    """Tables, HTML and the 〔ボタン: …〕 lines extract writes are not split; the
    finder does not see them."""
    return (all(l.lstrip().startswith("|") for l in lines) or lines[0].lstrip().startswith("<")
            or bool(MARK_LINE.match(lines[0].lstrip())))


def sentences(para):
    """Split one paragraph into sentences, keeping leading spaces. A ** right after
    the end mark opens the next bold when the sentence has an odd number of **."""
    out = [x for x in SENT.findall(para) if x]
    for k in range(len(out) - 1):
        if out[k].endswith("**") and out[k].count("**") % 2 == 1:
            out[k], out[k + 1] = out[k][:-2], "**" + out[k + 1]
    return out


def split_confirm(body):
    """(body before the confirmation list, the list) when the body ends with a list
    in the form assemble writes; (body, None) otherwise."""
    k = body.rfind("\n\n" + CONFIRM_HEAD)
    if k != -1:
        start = k + 2
    elif body.startswith(CONFIRM_HEAD):
        k = start = 0
    else:
        return body, None
    tail = body[start:].rstrip("\n")
    items = tail.split("\n")[4:]
    if not items or not all(CONFIRM_ITEM.match(l) for l in items):
        return body, None
    return body[:k], tail


def units(text):
    """Units of the body. Each has id, text, para, line (one unit per line)
    and raw (kept as is, not shown to the finder). A confirmation list that an
    earlier assemble wrote at the end is one raw unit with confirm, so a second
    round neither splits it nor reads it as text."""
    out = []
    body, box = split_confirm(split_front(text)[1])
    blocks = blocks_of(body)
    for pi, (gap, lines, code) in enumerate(blocks, 1):
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
    if box is not None:
        pi = len(blocks) + 1
        out.append({"para": pi, "gap": "\n\n" if blocks else "\n", "id": f"p{pi}-s1", "text": box,
                    "line": True, "raw": True, "confirm": True})
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
    meta = {"front": front, "source": str(Path(src).resolve())}
    write_jsonl(out / f"{stem}.units.jsonl", [meta] + us)
    write_text(out / f"{stem}.annotated.txt",
               "".join(f"[{u['id']}] {u['text'].strip()}\n" for u in us if not u.get("raw") and u["text"].strip()))
    rendered = render(us)
    write_text(out / f"{stem}.orig.md", front + rendered)
    ok = rendered == expected_body(text)
    print(json.dumps({"units": len(us), "round_trip": "ok" if ok else "DIFF",
                      "annotated": str(out / f"{stem}.annotated.txt")}, ensure_ascii=False))
    return 0 if ok else 1


def load_meta(path):
    """The first row without an id: {"front": ..., "source": ...} (may be empty)."""
    return next((r for r in read_jsonl(path) if "id" not in r), {})


def load_units(path):
    rows = read_jsonl(path)
    front = next((r.get("front", "") for r in rows if "id" not in r), "")
    return front, [r for r in rows if "id" in r]


# ---------- merge ----------

def reading_of(r):
    """A finding's "reading" (how the finder read a sentence that could be read more
    than one way), on one line; "" when it has none."""
    x = r.get("reading")
    return one_line(x.strip()) if isinstance(x, str) else ""


def merge_findings(rows):
    """Collapse several findings for one ID into one row.

    keep wins (the less destructive side when runs disagree), then rewrite, then
    delete. The row takes the earliest run's finding of that route; a rewrite
    carries that finding's "reading" when it has one.
    """
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    merged = []
    for i, rs in by_id.items():
        routes = {r["route"] for r in rs}
        route = next(x for x in ("keep", "rewrite", "delete") if x in routes)
        first = next(r for r in rs if r["route"] == route)
        m = {"id": i, "route": route, "fix": first.get("fix", "") if route == "rewrite" else "",
             "pattern": first.get("pattern", "")}
        if route == "rewrite" and reading_of(first):
            m["reading"] = reading_of(first)
        merged.append(m)
    return merged


def merge_findings_agree(rows, runs):
    """Edits only where every run proposed an edit for the ID. A keep from any run
    stays as a keep row, so the sentence is counted as kept."""
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    kept = []
    for rs in by_id.values():
        editors = {r.get("src", 0) for r in rs if r["route"] in EDIT_ROUTES}
        if any(r["route"] == "keep" for r in rs) or len(editors) < runs:
            rs = [r for r in rs if r["route"] == "keep"]
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


def join_error(r, us, pos):
    """(span, error) for a rewrite's "join". span is None when it joins nothing
    (no join, [], or just its own ID). A join must list IDs in a row, starting at
    "id", inside one prose paragraph."""
    j = r.get("join")
    if j is None or j == [] or j == [r.get("id")]:
        return None, None
    if (not isinstance(j, list) or len(j) < 2 or j[0] != r.get("id")
            or not all(isinstance(x, str) and x in pos for x in j)):
        return None, "join not contiguous"
    ks = [pos[x] for x in j]
    xs = [us[k] for k in ks]
    if ks != list(range(ks[0], ks[0] + len(ks))) or len({x["para"] for x in xs}) > 1:
        return None, "join not contiguous"
    if any(x.get("raw") or x.get("line") for x in xs):
        return None, "join over lines"
    return list(j), None


def resolve_joins(rows, mode, runs):
    """Pick the joins to use. Returns (accepted joins, the other rows, warnings).
    A join is dropped when a run keeps a sentence in it, and in agree mode when
    some other run edits nothing in it. Overlapping joins: the first run's wins.
    Edits of the sentences inside an accepted join are dropped."""
    warnings, accepted = [], []
    kept = {r["id"] for r in rows if r["route"] == "keep"}

    def touches(x, span):
        return x["id"] in span or bool(set(x.get("join") or ()) & set(span))

    for r in sorted((r for r in rows if r.get("join")), key=lambda r: r["src"]):
        span = r["join"]
        clash = next((a for a in accepted if set(a["join"]) & set(span)), None)
        if clash:
            if clash["join"] != span:
                warnings.append(f"run {r['src'] + 1}: join span differs {r['id']}")
            continue
        if kept & set(span):
            warnings.append(f"run {r['src'] + 1}: join kept {r['id']}")
            continue
        if mode == "agree":
            editors = {x["src"] for x in rows if x["route"] in EDIT_ROUTES and touches(x, span)}
            if len(editors) < runs:
                continue
        accepted.append(r)
    inside = {}
    for a in accepted:
        for j in a["join"]:
            inside[j] = a["id"]
    rest = []
    for r in rows:
        if r.get("join"):
            continue
        if r["route"] in EDIT_ROUTES and r["id"] in inside:
            if mode == "union":
                warnings.append(f"run {r['src'] + 1}: join {inside[r['id']]} drops {r['route']} {r['id']}")
            continue
        rest.append(r)
    return accepted, rest, warnings


def merge(us, runs, mode="agree"):
    """runs: one list of findings per finder run. Returns (merged, warnings)."""
    ids = {u["id"] for u in us if not u.get("raw")}
    pos = {u["id"]: k for k, u in enumerate(us)}
    warnings, rows = [], []
    for n, run in enumerate(runs):
        seen = set()
        for r in run:
            i, route = r.get("id"), r.get("route")
            if i not in ids or route not in ROUTES:
                warnings.append(f"run {n + 1}: skipped {i!r} (route {route!r})")
                continue
            if route == "rewrite" and "join" in r:
                span, err = join_error(r, us, pos)
                if err:
                    warnings.append(f"run {n + 1}: {err} {i}")
                    continue
                r = dict(r, join=span) if span else {k: v for k, v in r.items() if k != "join"}
            elif "join" in r:
                r = {k: v for k, v in r.items() if k != "join"}
            if (i, route) in seen:
                continue
            seen.add((i, route))
            rows.append(dict(r, src=n))
    conflicts, w = align_conflicts(rows)
    warnings += w
    joins, rest, w = resolve_joins(rows, mode, len(runs))
    warnings += w
    merged = merge_findings_agree(rest, len(runs)) if mode == "agree" else merge_findings(rest)
    # runs that aligned a sentence toward different words: the first run's rewrite
    # stands, and its reading says which words the runs chose
    for m in merged:
        if m["id"] in conflicts and m["route"] == "rewrite":
            m["reading"] = "／".join(x for x in (m.get("reading", ""), align_reading(conflicts[m["id"]])) if x)
    # a join takes the place of its first sentence's row
    for a in joins:
        head = next((m for m in merged if m["id"] == a["id"]), None)
        if head:
            merged.remove(head)
        m = {"id": a["id"], "route": "rewrite", "fix": a.get("fix", ""), "pattern": a.get("pattern", ""),
             "join": a["join"]}
        if reading_of(a):
            m["reading"] = reading_of(a)
        merged.append(m)
    if mode == "union" and len(runs) > 1:
        # a rewrite only one run proposed goes in, but is listed for a second look: a
        # single run's rewrite is where new actions or events slipped in during tests
        editors = {}
        for r in rows:
            if r["route"] == "rewrite":
                editors.setdefault(r["id"], set()).add(r["src"])
        for m in merged:
            if m.get("join"):
                n_editors = len({r["src"] for r in rows if r["route"] == "rewrite" and (
                    r["id"] in m["join"] or set(r.get("join") or ()) & set(m["join"]))})
            else:
                n_editors = len(editors.get(m["id"], ()))
            if m["route"] == "rewrite" and n_editors == 1:
                m["one_run"] = True
    # the bold check only speaks for sentences no finder run mentioned
    mentioned = {r["id"] for r in rows}
    for r in rows:
        mentioned.update(r.get("join") or ())
    merged += [b for b in bold_findings(us) if b["id"] not in mentioned]
    order = {u["id"]: k for k, u in enumerate(us)}
    merged.sort(key=lambda r: order[r["id"]])
    return merged, warnings


def align_key(word):
    """質問する and 質問します, 聞く and 聞きます compare equal: drop the trailing kana."""
    w = re.sub(r"[「」\s]", "", word or "")
    stem = re.sub(r"[ぁ-ゖ]+$", "", w)
    return stem or w


def align_conflicts(rows):
    """Rewrites of one ID from different runs that align toward different words are
    not an agreement on the word. A run's align word must also be in every other
    run's rewrite of that ID (a run that rewrote the sentence under another type
    still chose a word). The rewrites stay: merge uses the first run's, and its
    reading names the words. Returns ({id: words}, warnings)."""
    warnings, rewrites, words = [], {}, {}
    for r in rows:
        if r["route"] != "rewrite":
            continue
        rewrites.setdefault(r["id"], []).append(r)
        if not ALIGN_TYPES.search(r.get("pattern") or ""):
            continue
        w = re.sub(r"[「」]", "", str(r.get("align") or "")).strip()
        if not w:
            warnings.append(f"run {r['src'] + 1}: {r['id']} aligns a word but has no align")
            continue
        ws = words.setdefault(r["id"], [])
        if align_key(w) not in [align_key(x) for x in ws]:
            ws.append(w)
    conflicts = {}
    for i, ws in words.items():
        rs = rewrites[i]
        if len({r["src"] for r in rs}) < 2:
            continue
        missing = any(align_key(w) not in (r.get("fix") or "") for w in ws for r in rs)
        if len(ws) > 1 or missing:
            conflicts[i] = ws
            warnings.append(f"align conflict {i}: {' / '.join(ws)} -> first run")
    return conflicts, warnings


def align_reading(ws):
    """The reading for runs that aligned one sentence toward different words."""
    words = "・".join(f"「{w}」" for w in ws) if len(ws) > 1 else f"「{ws[0]}」とほかの回の言い方"
    return ALIGN_READING.format(words=words)


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

def added_words(new, orig_doc, orig_nums):
    """Cause, limit and time words, and numbers, in new that the whole original never had."""
    # 「まま」is not a time word: 「書式のままです」 holds 「まで」 only across the two words
    probe, src = new.replace("まま", "＿＿"), orig_doc.replace("まま", "＿＿")
    hits = [m for m in ADDED_MARKERS + TIME_MARKERS if m in probe and m not in src]
    hits += [n for n in NUM.findall(new) if n not in orig_nums]
    return hits


def assemble(us, merged):
    """Returns (body, log). us is changed in place."""
    by_id = {u["id"]: u for u in us}
    pos = {u["id"]: k for k, u in enumerate(us)}
    # the original to compare against: the body without an earlier round's list,
    # whose words are not the writer's text
    box = next((u for u in us if u.get("confirm")), None)
    orig_doc = "".join(u["text"] for u in us if not u.get("confirm"))
    orig_nums = set(NUM.findall(orig_doc))
    before = {u["id"]: u["text"] for u in us}
    changes, reverted, readings, one_run, skipped = [], [], [], [], []
    joined = set()  # sentences a join folded into the sentence before them
    for f in merged:
        u = by_id.get(f.get("id"))
        span, err = join_error(f, us, pos) if f.get("route") == "rewrite" else (None, None)
        if not u or u.get("raw") or err or f.get("route") not in ROUTES or f["id"] in joined:
            skipped.append(f.get("id"))
            continue
        old = u["text"]
        if f["route"] == "keep":
            changes.append({"id": f["id"], "route": "keep", "result": "残した",
                            "pattern": f.get("pattern", ""), "before": old.strip(), "after": old.strip()})
            continue
        lead = old[: len(old) - len(old.lstrip())]
        # trailing spaces (a Markdown hard break at the end of a line) stay where they were
        trail = old[len(old.rstrip()):]
        prefix = BLOCK_START.match(old.lstrip())
        prefix = prefix.group(1) if prefix else ""
        put_back = False
        if f["route"] == "delete":
            new = ""
        else:
            new = (f.get("fix") or "").strip()
            hits = added_words(new, orig_doc, orig_nums)
            if hits or not new:
                # a join that is put back puts back every sentence in it
                reverted.append(dict({"id": f["id"], "proposed": new, "words": hits or ["空の言い換え"]},
                                     **({"join": span} if span else {})))
                new, span, put_back = old.strip(), None, True
        if new and prefix and not new.startswith(prefix.strip()):
            new = prefix + new
        if span:
            # the rest of the join goes as a deleted sentence does
            old_span = "".join(by_id[j]["text"] for j in span)
            for j in span[1:]:
                by_id[j]["text"] = ""
                joined.add(j)
            u["text"] = lead + new + trail
            changes.append({"id": f["id"], "route": f["route"], "result": "変えた",
                            "pattern": f.get("pattern", ""),
                            "before": old_span.strip(), "after": u["text"].strip(), "join": span})
        else:
            u["text"] = (lead + new + trail) if new else ""
            if u["text"] == old:
                continue
            changes.append({"id": f["id"], "route": f["route"], "result": "変えた",
                            "pattern": f.get("pattern", ""),
                            "before": old.strip(), "after": u["text"].strip()})
        if f["route"] == "rewrite" and not put_back:
            after = u["text"].strip()
            if isinstance(f.get("reading"), str) and f["reading"].strip():
                readings.append({"id": f["id"], "reading": one_line(f["reading"].strip()), "after": after})
            if f.get("one_run"):
                one_run.append({"id": f["id"], "after": after})

    dangling = dangling_refs(us, before, joined)
    orphans = orphan_paragraphs(us, before, joined)
    lost = lost_words(us, before, joined, changes, dangling)
    readings.sort(key=lambda x: pos[x["id"]])
    one_run.sort(key=lambda x: pos[x["id"]])
    # the list at the end: how sentences that could be read two ways were read, the
    # rewrites only one run proposed, then what deletions left behind
    lines = "".join([reading_line(x) for x in readings] + [one_run_line(x) for x in one_run]
                    + [dangling_line(d) for d in dangling] + [lost_line(x) for x in lost]
                    + [orphan_line(o) for o in orphans])
    if box is not None and lines:
        # an earlier round's list takes the new lines at its end: one list, one heading
        box["text"] += "\n" + lines.rstrip("\n")
    body = render(us)
    if box is None and lines:
        body += "\n" + CONFIRM_HEAD + lines
    edited = [c for c in changes if c["route"] != "keep"]
    log = {"changes": changes, "reverted": reverted, "readings": readings, "one_run": one_run,
           "dangling": dangling, "lost": lost, "orphans": orphans,
           "skipped": [s for s in skipped if s], "unchanged_ids": len(before) - sum(len(c.get("join") or [c]) for c in edited)}
    return body, log


def reading_line(x):
    return f"- 読みを選んだ: {x['reading']}（{x['id']}「{one_line(x['after'])}」）\n"


def one_run_line(x):
    return f"- 片方の回だけが直した文: 原文に無い行動・出来事が増えていないか（{x['id']}「{one_line(x['after'])}」）\n"


def deleted_ids(us, before, joined=()):
    """Sentences that were deleted (a sentence a join folded away was rewritten, not deleted)."""
    return [u["id"] for u in us if before.get(u["id"]) and before[u["id"]].strip() and not u["text"]
            and u["id"] not in joined]


def dangling_refs(us, before, joined=()):
    """Words that appear only in a deleted sentence, while a later remaining sentence
    points at them (両課 -> 経理課・営業課, この方式 -> 新方式). Reported, not fixed.

    A word is a candidate when it is in a deleted sentence and nowhere in the remaining
    body; it is reported when a pointing word's head noun in a later remaining sentence
    is part of the word (課 in 経理課, 方式 in 新方式)."""
    order = {u["id"]: k for k, u in enumerate(us)}
    out = []
    for i in deleted_ids(us, before, joined):
        words = only_words(us, before, i)
        if not words:
            continue
        for u in us[order[i] + 1:]:
            if not u["text"] or u.get("raw"):
                continue
            for m in POINTER.finditer(u["text"]):
                head = m.group(1)
                # a one-character head (両課, 同社) matches the word ending in it; a longer
                # head (この方式) must appear in the word, so 設定 does not catch 特定
                hits = [w for w in words if head in w]
                if hits:
                    out.append({"deleted": i, "words": hits, "at": u["id"], "ref": m.group(0)})
    return out


def dangling_line(d):
    return (f"- 削った文が指していた語が残っていません: {'・'.join(d['words'])}"
            f"（{d['at']} の「{d['ref']}」が受けている。{d['deleted']} を削った）\n")


def only_words(us, before, i):
    """Nouns of the deleted sentence i found nowhere in the remaining body."""
    remaining = "".join(u["text"] for u in us if not u.get("confirm"))
    words = []
    for w in NOUN.findall(unbold(before[i])):
        if w not in remaining and w not in words:
            words.append(w)
    return words


def lost_words(us, before, joined, changes, dangling=()):
    """Sentences deleted as a restatement (型 6) that held a word found nowhere else
    in the body (残高 in 口座の残高を読む機能ではありません): the sentence was not only a
    restatement. Words already reported as dangling are left out. Reported, not put back."""
    restate = {c["id"] for c in changes if c["route"] == "delete" and RESTATE.match(c.get("pattern", ""))}
    out = []
    for i in deleted_ids(us, before, joined):
        if i not in restate:
            continue
        told = {w for d in dangling if d["deleted"] == i for w in d["words"]}
        words = [w for w in only_words(us, before, i) if w not in told]
        if words:
            out.append({"deleted": i, "words": words, "text": before[i].strip()})
    return out


def lost_line(x):
    return f"- 言い直しとして削った文にしか無い語: {'・'.join(x['words'])}（{x['deleted']}「{x['text']}」）\n"


def orphan_paragraphs(us, before, joined=()):
    """Prose paragraphs that had two or more sentences and keep exactly one after
    deletions. Lists, headings, quotes, tables and code are not prose paragraphs.
    A text that is down to one paragraph has no neighbours to float among, so
    nothing is reported then."""
    paras = {}
    for u in us:
        paras.setdefault(u["para"], []).append(u)
    out = []
    if sum(1 for xs in paras.values() if any(x["text"].strip() and not x.get("confirm") for x in xs)) < 2:
        return out
    for p, xs in paras.items():
        if any(x.get("raw") or BLOCK_START.match(before[x["id"]].lstrip()) for x in xs):
            continue
        had = [x for x in xs if before[x["id"]].strip() and x["id"] not in joined]
        left = [x for x in xs if x["text"].strip()]
        if len(had) >= 2 and len(left) == 1:
            out.append({"para": p, "id": left[0]["id"], "text": left[0]["text"].strip(),
                        "deleted": [x["id"] for x in had if not x["text"].strip()]})
    return out


def orphan_line(o):
    return f"- 1 文だけ残った段落: 〔{o['text']}〕（{o['id']}）\n"


def output_path(source):
    """<stem>.japango.md next to source, numbered when the name is taken."""
    src = Path(source)
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        cand = src.with_name(f"{src.stem}.japango{suffix}.md")
        if not cand.exists() and cand.resolve() != src.resolve():
            return cand
        n += 1


def cmd_assemble(units_path, merged_path, log_path, out_path=None):
    source = load_meta(units_path).get("source")
    if out_path is None:
        if not source:
            print("assemble: the units file has no source path; give --out", file=sys.stderr)
            return 2
        out_path = output_path(source)
    elif source and Path(out_path).resolve() == Path(source).resolve():
        print("assemble: --out is the source itself; the source is never overwritten", file=sys.stderr)
        return 2
    front, us = load_units(units_path)
    body, log = assemble(us, read_jsonl(merged_path))
    write_text(out_path, front + body)
    write_text(log_path, json.dumps(log, ensure_ascii=False, indent=1) + "\n")
    kept = sum(1 for c in log["changes"] if c["route"] == "keep")
    print(json.dumps({"changed": len(log["changes"]) - kept, "kept": kept,
                      "reverted": len(log["reverted"]), "readings": len(log["readings"]),
                      "one_run": len(log["one_run"]),
                      "dangling": len(log["dangling"]), "lost": len(log["lost"]), "orphans": len(log["orphans"]),
                      "out": str(out_path)}, ensure_ascii=False))
    return 0


# ---------- extract ----------

# a hand-written reader, not html.parser, so japango.mjs can read a page the same way
# (white space is HTML's, the ASCII five, in every pattern here)
TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:[ \t\n\r\f]+[^ \t\n\r\f\"'>/=]+(?:[ \t\n\r\f]*=[ \t\n\r\f]*"
                 r"(?:\"[^\"]*\"|'[^']*'|[^ \t\n\r\f\"'>]+))?)*)[ \t\n\r\f]*(/?)[ \t\n\r\f]*>")
ATTR = re.compile(r"([^ \t\n\r\f\"'>/=]+)(?:[ \t\n\r\f]*=[ \t\n\r\f]*(?:\"([^\"]*)\"|'([^']*)'|([^ \t\n\r\f\"'>]+)))?")
ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z]+);")
ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ", "copy": "©",
            "hellip": "…", "mdash": "—", "ndash": "–", "laquo": "«", "raquo": "»", "times": "×",
            "yen": "¥", "middot": "·", "rarr": "→", "larr": "←"}
# only ASCII white space folds; a full-width space is part of the text
HTML_WS = re.compile(r"[ \t\n\r\f]+")
# elements whose content is not text to read
SKIP_TAGS = {"head", "script", "style", "svg", "template", "noscript", "iframe", "canvas", "object", "select"}
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "dialog", "div",
              "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
              "h6", "header", "hr", "html", "li", "main", "nav", "ol", "p", "section", "summary",
              "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"}
HEADING = re.compile(r"h[1-6]")
# a line break between two Japanese characters, which a page wraps without a space
CJK = "　-ヿ㐀-鿿豈-﫿＀-￯"
CJK_BREAK = re.compile(f"(?<=[{CJK}])[ \\t\\r\\f]*\\n[ \\t\\n\\r\\f]*(?=[{CJK}])")


def unescape(t):
    def one(m):
        e = m.group(1)
        if e[0] == "#":
            n = int(e[2:], 16) if e[1] in "xX" else int(e[1:])
            return chr(n) if 0 < n < 0x110000 and not 0xD800 <= n < 0xE000 else m.group(0)
        return ENTITIES.get(e, m.group(0))
    return ENTITY.sub(one, t)


def ascii_lower(t):
    return re.sub("[A-Z]", lambda m: m.group(0).lower(), t)


def html_tokens(html):
    """("text", s), ("open", name, attrs, self_closing) and ("close", name), in order.
    Comments and <!...> are dropped; the content of script and style is skipped whole."""
    out, k, n = [], 0, len(html)
    while k < n:
        lt = html.find("<", k)
        if lt == -1:
            out.append(("text", html[k:]))
            break
        if lt > k:
            out.append(("text", html[k:lt]))
        if html.startswith("<!--", lt):
            end = html.find("-->", lt + 4)
            k = n if end == -1 else end + 3
            continue
        if html.startswith("<!", lt) or html.startswith("<?", lt):
            end = html.find(">", lt)
            k = n if end == -1 else end + 1
            continue
        m = TAG.match(html, lt)
        if not m:
            out.append(("text", "<"))
            k = lt + 1
            continue
        name = ascii_lower(m.group(2))
        k = m.end()
        if m.group(1):
            out.append(("close", name))
            continue
        attrs = {}
        for a in ATTR.finditer(m.group(3)):
            v = next((x for x in a.groups()[1:] if x is not None), "")
            attrs.setdefault(ascii_lower(a.group(1)), unescape(v))
        closed = bool(m.group(4))
        out.append(("open", name, attrs, closed))
        if name in ("script", "style") and not closed:
            end = re.compile("</" + "".join(f"[{c}{c.upper()}]" for c in name)).search(html, k)
            k = n if end is None else end.start()
    return out


def fold(t):
    return HTML_WS.sub(" ", t).strip(" ")


def is_button(name, attrs):
    return name == "button" or (name == "a" and "btn" in HTML_WS.split(attrs.get("class", "")))


def extract(html):
    """The text of an HTML page as Markdown for ids. Returns (markdown, counts).

    Headings become #, list items "- ", and p, dt, dd and table cells paragraphs;
    head, script, style and svg go. A button (button, or an a with class btn)
    becomes 〔ボタン: text → href〕, pre and code 〔コード: …〕, and img and video
    with alt, aria-label, title or poster 〔画像: …〕, each on a line of its own
    (ids keeps a paragraph of them as is). code inside a sentence stays in it as `…`."""
    blocks = []      # [kind, text, flush number]: kind "text", "li" or "mark"
    pieces = []      # ("t", text), ("br",), ("m", line), ("c", code)
    stack = []       # open block elements
    state = {"skip": 0, "cap": None, "flushes": 0}  # cap: [name, attrs, text] inside a button, code or pre
    counts = {"ボタン": 0, "コード": 0, "画像": 0}

    def mark(kind, t):
        counts[kind] += 1
        return f"〔{kind}: {t}〕"

    def flush():
        heading = next((x for x in reversed(stack) if HEADING.fullmatch(x)), None)
        item = next((x for x in reversed(stack) if x in ("li", "dt", "dd", "td", "th")), None)
        in_text = any(p[0] == "t" and fold(p[1]) for p in pieces)
        segs, cur = [], []

        def end_text():
            lines = [x for x in (fold(x) for x in CJK_BREAK.sub("", "".join(cur)).split("\0")) if x]
            if lines and heading:
                segs.append(["text", "#" * int(heading[1]) + " " + "".join(lines)])
            elif lines:
                segs.append(["li", "- " + "\n".join(lines)] if item == "li" else ["text", "\n".join(lines)])
            cur.clear()
        for p in pieces:
            if p[0] == "t":
                # a line break in the source is a space (none between Japanese
                # characters); only <br>, here \0, breaks the line
                cur.append(p[1].replace("\0", ""))
            elif p[0] == "br":
                cur.append("\0")
            elif p[0] == "c" and in_text:
                cur.append("`" + fold(p[1]) + "`")
            else:
                end_text()
                segs.append(["mark", p[1] if p[0] == "m" else mark("コード", fold(p[1]))])
        end_text()
        pieces.clear()
        n = state["flushes"]
        for kind, t in segs:
            # marks next to each other in one block stay one paragraph, and so do list items
            if blocks and blocks[-1][0] == kind and (kind == "li" or kind == "mark" and blocks[-1][2] == n):
                blocks[-1][1] += "\n" + t
            else:
                blocks.append([kind, t, n])
        state["flushes"] = n + 1

    def end_capture():
        name, a, t = state["cap"]
        state["cap"] = None
        if name == "pre":
            # a blank line would end the paragraph ids keeps as is
            t = "\n".join(x for x in t.strip("\n").split("\n") if x.strip(" \t\r\f"))
            if t:
                pieces.append(("m", mark("コード", t)))
            flush()
        elif name == "code" and fold(t):
            pieces.append(("c", t))
        elif name != "code":
            t = fold(t) or fold(a.get("aria-label") or a.get("title") or a.get("value") or "")
            href = a.get("href", "")
            if t:
                pieces.append(("m", mark("ボタン", t + (" → " + href if href else ""))))

    for tok in html_tokens(html):
        kind = tok[0]
        name = tok[1] if kind != "text" else ""
        if state["skip"]:
            if kind == "open" and name in SKIP_TAGS and not tok[3]:
                state["skip"] += 1
            elif kind == "close" and name in SKIP_TAGS:
                state["skip"] -= 1
            continue
        cap = state["cap"]
        if kind == "open" and name in SKIP_TAGS:
            if not tok[3]:
                state["skip"] = 1
            continue
        if cap is not None:
            if kind == "text":
                cap[2] += unescape(tok[1])
            elif kind == "open" and name == "br":
                cap[2] += "\n"
            elif kind == "close" and name == cap[0]:
                end_capture()
            continue
        if kind == "text":
            pieces.append(("t", unescape(tok[1])))
        elif kind == "open":
            attrs, closed = tok[2], tok[3]
            if name == "br":
                pieces.append(("br",))
            elif name in ("img", "video"):
                t = fold(attrs.get("alt") or attrs.get("aria-label") or attrs.get("title") or attrs.get("poster") or "")
                if t:
                    pieces.append(("m", mark("画像", t)))
            elif closed:
                if name in BLOCK_TAGS:
                    flush()
            elif name == "pre":
                flush()
                state["cap"] = ["pre", attrs, ""]
            elif name == "code" or is_button(name, attrs):
                state["cap"] = [name, attrs, ""]
            elif name in BLOCK_TAGS:
                flush()
                if name != "hr":
                    stack.append(name)
        elif name in BLOCK_TAGS:
            flush()
            if name in stack:
                del stack[len(stack) - 1 - stack[::-1].index(name):]
    if state["cap"] is not None:
        end_capture()
    flush()
    counts["段落"] = len(blocks)
    md = "\n\n".join(b[1] for b in blocks)
    return (md + "\n" if md else ""), counts


def cmd_extract(src, out):
    if Path(out).resolve() == Path(src).resolve():
        print("extract: -o is the page itself; the page is never overwritten", file=sys.stderr)
        return 2
    md, counts = extract(read_text(src))
    write_text(out, md)
    print(json.dumps({"paragraphs": counts["段落"], "buttons": counts["ボタン"],
                      "code": counts["コード"], "images": counts["画像"], "out": str(out)}, ensure_ascii=False))
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
    s.add_argument("log_json")
    s.add_argument("--out", dest="out_md", default=None,
                   help="write here instead of <source stem>.japango.md next to the source")
    x = sub.add_parser("extract", help="take the text out of an HTML page")
    x.add_argument("src")
    x.add_argument("-o", dest="out", required=True, help="the Markdown to write")
    args = p.parse_args(argv)
    if args.cmd == "extract":
        return cmd_extract(args.src, args.out)
    if args.cmd == "ids":
        return cmd_ids(args.src, args.workdir)
    if args.cmd == "merge":
        return cmd_merge(args.units, args.out, args.mode, args.findings)
    return cmd_assemble(args.units, args.merged, args.log_json, args.out_md)


if __name__ == "__main__":
    sys.exit(main())
