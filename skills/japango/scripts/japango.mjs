// japango: number sentences, merge finder runs, assemble the revised text.
//
// Node.js 18+ port of japango.py, for machines where Python is missing or
// cannot be started. Built-in modules only. japango.py is the reference: the
// same commands, arguments and files, and byte-for-byte the same output
// (tests/test_parity.py checks this). Change both together.
//
//   ids SRC WORKDIR
//   merge UNITS OUT MODE FINDINGS [FINDINGS ...]
//   assemble UNITS MERGED LOG_JSON [--out OUT_MD]
//
// See japango.py for what each command does.
import fs from "node:fs";
import path from "node:path";

// a sentence ends at 。！？ plus the closing marks that belong to it (bold, brackets)
const SENT = /[^。！？]*[。！？](?:\*\*|[」』）)])*|[^。！？]+$/gu;
// Python's \d and \s are Unicode-aware; \p{Nd} and JS \s match them closely enough
const BLOCK_START = /^(#{1,6} |[-*+] |\p{Nd}+[.)] |> )/u;
const FENCE = /^\s*(```|~~~)/u;

const ROUTES = ["delete", "rewrite", "ask", "keep"];
const EDIT_ROUTES = ["rewrite", "delete"];
// types whose rewrite aligns a word with the rest of the text (呼び名・人の呼び名・動詞)
const ALIGN_TYPES = /型\s*21[de]?(?![0-9a-z])/u;
const ALIGN_ASK = (words) => `〔${words}のどれにそろえるか〕`;
const ALIGN_ASK1 = (word) => `〔「${word}」とほかの回の言い方のどちらにそろえるか〕`;
// a line that is only a URL: a mark after it would become part of the link
const URL_LINE = /^<?https?:\/\/\S+>?$/u;

// words that state a cause or a limit, and words that bound a time
const ADDED_MARKERS = ["だけ", "のみ", "紛れ", "ため", "により", "によって", "原因", "せいで"];
const SINGLE_REWRITE_ASK = (head) => `〔「${head}」の書き直しで、原文に無い行動・出来事が増えていないか〕`;
const TIME_MARKERS = ["まで", "以降", "以前", "までに"];
const NUM = /\p{Nd}+(?:\.\p{Nd}+)?/gu;
const BOLD = /\*\*(.+?)\*\*/gu;

// questions about deadlines or owners of future work are not what the text is missing;
// keep questions about who decided, why, and what caused it
const ASK_KEEP = /誰|なぜ|理由|根拠|原因|どの|何を|出典/u;
const ASK_DROP = /いつまで|期限|期日|予定/u;

// nouns for the dangling-reference check: runs of 2+ kanji or katakana
const NOUN = /[一-鿿々〆ヶァ-ヴー]{2,}/gu;
// a pointing word and the head noun right after it (両課, この方式, 同社, 前述の手順)
const POINTER = new RegExp(
  "(?:(?<![\\u4E00-\\u9FFF々])(?:両|同|当該|上記の?|前述の?|先の)|この|その|これらの|それらの)" +
  "([\\u4E00-\\u9FFF々〆ヶァ-ヴー]{1,6})", "gu");

// ---------- Python-compatible helpers ----------

const WS = /^[\s\x1c-\x1f\x85]+|[\s\x1c-\x1f\x85]+$/gu;
const strip = (s) => s.replace(WS, "");
const lstrip = (s) => s.replace(/^[\s\x1c-\x1f\x85]+/u, "");
const rstrip = (s) => s.replace(/[\s\x1c-\x1f\x85]+$/u, "");
const stripChars = (s, c, left = true, right = true) => {
  let a = 0, b = s.length;
  if (left) while (a < b && s[a] === c) a++;
  if (right) while (b > a && s[b - 1] === c) b--;
  return s.slice(a, b);
};
const findall = (re, s) => [...s.matchAll(re)].map((m) => (m.length > 1 ? m[1] : m[0]));

// str.splitlines() boundaries; built from a string, since U+2028/2029 cannot sit in a regex literal
const LINE_BREAK = new RegExp("\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]", "u");

function splitlines(s) {
  const out = s.split(LINE_BREAK);
  if (out.length && out[out.length - 1] === "") out.pop();
  return out;
}

function pyRepr(v) {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "string") {
    const q = v.includes("'") && !v.includes('"') ? '"' : "'";
    const body = v.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t");
    return q + (q === "'" ? body.replace(/'/g, "\\'") : body) + q;
  }
  if (Array.isArray(v)) return "[" + v.map(pyRepr).join(", ") + "]";
  if (typeof v === "object") return "{" + Object.entries(v).map(([k, x]) => `${pyRepr(k)}: ${pyRepr(x)}`).join(", ") + "}";
  return String(v);
}

// json.dumps(ensure_ascii=False), with indent=None or a number
function dumps(v, indent = null, level = 0) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string" || typeof v === "boolean") return JSON.stringify(v);
  if (typeof v === "number") return Number.isInteger(v) && !Object.is(v, -0) ? String(v) : JSON.stringify(v);
  const nl = indent === null ? "" : "\n" + " ".repeat(indent * (level + 1));
  const end = indent === null ? "" : "\n" + " ".repeat(indent * level);
  const sep = indent === null ? ", " : ",";
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return "[" + v.map((x) => nl + dumps(x, indent, level + 1)).join(sep) + end + "]";
  }
  const ks = Object.keys(v);
  if (!ks.length) return "{}";
  return "{" + ks.map((k) => nl + JSON.stringify(k) + ": " + dumps(v[k], indent, level + 1)).join(sep) + end + "}";
}

const isDigit = (c) => /^\p{Nd}$/u.test(c);
const isAlpha = (c) => /^\p{L}$/u.test(c);
const isAscii = (c) => [...c].every((x) => x.codePointAt(0) < 128);

// ---------- files ----------

function writeText(p, s) {
  fs.writeFileSync(p, s, { encoding: "utf8" });
}

function readText(p) {
  let s = fs.readFileSync(p, "utf8");
  if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
  return s.replace(/\r\n/g, "\n");
}

// Rows of a JSON-lines file. Lines that are not JSON objects are skipped
// (and counted in warn, a list, when given).
function readJsonl(p, warn = null) {
  const rows = [];
  splitlines(readText(p)).forEach((raw, k) => {
    const line = strip(raw);
    if (!line.startsWith("{")) return;
    try {
      rows.push(JSON.parse(line));
    } catch {
      if (warn !== null) warn.push(`${p}:${k + 1}: not JSON`);
    }
  });
  return rows;
}

function writeJsonl(p, rows) {
  writeText(p, rows.map((r) => dumps(r) + "\n").join(""));
}

// ---------- ids ----------

// [front matter including the blank lines after it, body]
function splitFront(text) {
  if (text.startsWith("---\n")) {
    const end = text.indexOf("\n---\n", 4);
    if (end !== -1) {
      const cut = end + 5;
      const rest = text.slice(cut);
      const body = stripChars(rest, "\n", true, false);
      return [text.slice(0, cut) + rest.slice(0, rest.length - body.length), body];
    }
  }
  const body = stripChars(text, "\n", true, false);
  return [text.slice(0, text.length - body.length), body];
}

// [[gap, lines, isCode]]: paragraphs split at blank lines, fenced code kept whole.
// gap is the exact text that separated the block from the previous one.
function blocksOf(body) {
  const out = [];
  let cur = [], gap = [], fence = false;
  const gapText = () => ["", ...gap, ""].join("\n");
  for (const line of stripChars(body, "\n", false, true).split("\n")) {
    if (fence) {
      cur.push(line);
      if (FENCE.test(line)) {
        out.push([gapText(), cur, true]);
        cur = []; gap = []; fence = false;
      }
      continue;
    }
    if (FENCE.test(line)) {
      if (cur.length) {
        out.push([gapText(), cur, false]);
        gap = [];
      }
      cur = [line]; fence = true;
      continue;
    }
    if (!strip(line)) {
      if (cur.length) {
        out.push([gapText(), cur, false]);
        cur = []; gap = [];
      }
      gap.push(line);
      continue;
    }
    cur.push(line);
  }
  if (cur.length) out.push([gapText(), cur, fence]);
  return out;
}

// Tables and HTML are not split; the finder does not see them.
function isRaw(lines) {
  return lines.every((l) => lstrip(l).startsWith("|")) || lstrip(lines[0]).startsWith("<");
}

const count = (s, sub) => s.split(sub).length - 1;

// Split one paragraph into sentences, keeping leading spaces. A ** right after
// the end mark opens the next bold when the sentence has an odd number of **.
function sentences(para) {
  const out = findall(SENT, para).filter((x) => x);
  for (let k = 0; k < out.length - 1; k++) {
    if (out[k].endsWith("**") && count(out[k], "**") % 2 === 1) {
      [out[k], out[k + 1]] = [out[k].slice(0, -2), "**" + out[k + 1]];
    }
  }
  return out;
}

// Units of the body. Each has id, text, para, line (one unit per line)
// and raw (kept as is, not shown to the finder).
function units(text) {
  const out = [];
  blocksOf(splitFront(text)[1]).forEach(([gap, lines, code], k) => {
    const pi = k + 1;
    const base = { para: pi, gap };
    if (code || isRaw(lines)) {
      out.push({ ...base, id: `p${pi}-s1`, text: lines.join("\n"), line: true, raw: true });
      return;
    }
    if (lines.length > 1 || BLOCK_START.test(lines[0])) {
      lines.forEach((l, si) => out.push({ ...base, id: `p${pi}-s${si + 1}`, text: l, line: true }));
      return;
    }
    // keep each sentence's leading spaces so rendering restores the original
    sentences(lines[0]).forEach((s, si) => out.push({ ...base, id: `p${pi}-s${si + 1}`, text: s, line: false }));
  });
  return out;
}

// Join units back into text. Empty units (deleted) drop out; a paragraph
// whose units are all empty drops out with its gap.
function render(us) {
  const paras = [];
  for (const u of us) {
    if (!paras.length || paras[paras.length - 1][0] !== u.para) {
      paras.push([u.para, u.gap ?? "\n\n", u.line ?? false, []]);
    }
    if (u.text) paras[paras.length - 1][3].push(u.text);
  }
  let out = "";
  for (const [, gap, isLine, texts] of paras) {
    if (!texts.length) continue;
    const block = texts.join(isLine ? "\n" : "");
    out += (out ? gap : "") + block;
  }
  return out + "\n";
}

const expectedBody = (text) => stripChars(splitFront(text)[1], "\n", false, true) + "\n";

function resolve(p) {
  try {
    return fs.realpathSync.native(p);
  } catch {
    return path.resolve(p);
  }
}

const stemOf = (p) => path.parse(p).name;

function cmdIds(src, workdir) {
  const text = readText(src);
  const us = units(text);
  fs.mkdirSync(workdir, { recursive: true });
  const stem = stemOf(src);
  const front = splitFront(text)[0];
  const meta = { front, source: resolve(src) };
  const at = (name) => path.join(workdir, name);
  writeJsonl(at(`${stem}.units.jsonl`), [meta, ...us]);
  writeText(at(`${stem}.annotated.txt`),
    us.filter((u) => !u.raw && strip(u.text)).map((u) => `[${u.id}] ${strip(u.text)}\n`).join(""));
  const rendered = render(us);
  writeText(at(`${stem}.orig.md`), front + rendered);
  const ok = rendered === expectedBody(text);
  console.log(dumps({ units: us.length, round_trip: ok ? "ok" : "DIFF", annotated: at(`${stem}.annotated.txt`) }));
  return ok ? 0 : 1;
}

// The first row without an id: {"front": ..., "source": ...} (may be empty).
function loadMeta(p) {
  return readJsonl(p).find((r) => !("id" in r)) ?? {};
}

function loadUnits(p) {
  const rows = readJsonl(p);
  const meta = rows.find((r) => !("id" in r));
  return [meta ? (meta.front ?? "") : "", rows.filter((r) => "id" in r)];
}

// ---------- merge ----------

const useful = (q) => Boolean(q) && !(ASK_DROP.test(q) && !ASK_KEEP.test(q));

// 〔誰が判断したか〕〔根拠は何か〕 -> ["誰が判断したか", "根拠は何か"].
// Text without brackets is one question.
function splitAsks(q) {
  q = strip(q || "");
  const parts = findall(/〔([^〔〕]*)〕/gu, q).map(strip).filter((x) => x);
  if (!parts.length) return q ? [q] : [];
  return parts;
}

// The questions of one finding worth asking, joined back as 〔..〕〔..〕.
const usefulAsks = (q) => splitAsks(q).filter(useful).map((x) => `〔${x}〕`).join("");

const get = (r, k, d) => (k in r ? r[k] : d);

function groupById(rows) {
  const byId = new Map();
  for (const r of rows) {
    if (!byId.has(r.id)) byId.set(r.id, []);
    byId.get(r.id).push(r);
  }
  return byId;
}

// Collapse several findings for one ID into one row; see merge_findings in japango.py.
function mergeFindings(rows) {
  const merged = [];
  for (const [i, rs] of groupById(rows)) {
    const routes = new Set(rs.map((r) => r.route));
    const askRows = rs.filter((r) => r.route === "ask" && r.fix);
    const firstSrc = askRows.length ? Math.min(...askRows.map((r) => get(r, "src", 0))) : null;
    const asks = askRows.filter((r) => get(r, "src", 0) === firstSrc).map((r) => r.fix).join("");
    if (routes.has("keep") && asks) {
      merged.push({ id: i, route: "ask", fix: asks });
    } else if (routes.has("keep")) {
      const first = rs.find((r) => r.route === "keep");
      merged.push({ id: i, route: "keep", fix: "", pattern: get(first, "pattern", "") });
    } else if (routes.has("rewrite")) {
      const first = rs.find((r) => r.route === "rewrite");
      merged.push({ id: i, route: "rewrite", fix: get(first, "fix", ""), ask: asks, pattern: get(first, "pattern", "") });
    } else if (routes.has("delete")) {
      const first = rs.find((r) => r.route === "delete");
      merged.push({ id: i, route: "delete", fix: "", ask: asks, pattern: get(first, "pattern", "") });
    } else {
      merged.push({ id: i, route: "ask", fix: asks });
    }
  }
  return merged;
}

// Edits only where every run proposed an edit for the ID; questions are the union.
// A keep from any run stays as a keep row, so the sentence is counted as kept.
function mergeFindingsAgree(rows, runs) {
  let kept = [];
  for (let rs of groupById(rows).values()) {
    const editors = new Set(rs.filter((r) => EDIT_ROUTES.includes(r.route)).map((r) => get(r, "src", 0)));
    if (rs.some((r) => r.route === "keep") || editors.size < runs) {
      rs = rs.filter((r) => r.route === "ask" || r.route === "keep");
    }
    kept = kept.concat(rs);
  }
  return mergeFindings(kept);
}

// Drop ** but keep a space between kana/kanji and a digit, as many documents do.
function unbold(t) {
  return t.replace(BOLD, (m, inner, start) => {
    const end = start + m.length;
    const before = start ? t[start - 1] : "";
    const after = end < t.length ? t[end] : "";
    const lead = before && isAlpha(before) && !isAscii(before) && inner && isDigit([...inner][0]) ? " " : "";
    const tail = after && isAlpha(after) && !isAscii(after) && inner && isDigit([...inner].at(-1)) ? " " : "";
    return lead + inner + tail;
  });
}

// Bold that wraps a whole sentence, or more than one bold span in a sentence.
function boldFindings(us) {
  const out = [];
  for (const u of us) {
    const t = strip(u.text);
    if (u.raw || t.startsWith("#")) continue;
    const spans = findall(BOLD, t);
    const whole = t.startsWith("**") && stripChars(t, "。", false, true).endsWith("**") && spans.length === 1;
    if (whole || spans.length > 1) out.push({ id: u.id, route: "rewrite", fix: unbold(t), pattern: "太字" });
  }
  return out;
}

// runs: one list of findings per finder run. Returns [merged, warnings].
function merge(us, runs, mode = "agree") {
  const ids = new Set(us.filter((u) => !u.raw).map((u) => u.id));
  const warnings = [], rows = [];
  runs.forEach((run, n) => {
    const seen = new Set();
    for (let r of run) {
      const i = r.id, route = r.route;
      if (typeof i !== "string" || !ids.has(i) || !ROUTES.includes(route)) {
        warnings.push(`run ${n + 1}: skipped ${pyRepr(i)} (route ${pyRepr(route)})`);
        continue;
      }
      const key = i + "\u0000" + route;
      if (seen.has(key)) continue;
      seen.add(key);
      if (route === "ask") {
        r = { ...r, fix: usefulAsks(r.fix) };
        if (!r.fix) continue;
      }
      rows.push({ ...r, src: n });
    }
  });
  const [aligned, w] = alignConflicts(rows);
  warnings.push(...w);
  const merged = mode === "agree" ? mergeFindingsAgree(aligned, runs.length) : mergeFindings(aligned);
  if (mode === "union" && runs.length > 1) {
    // a rewrite only one run proposed goes in, but the writer checks it: a single
    // run's rewrite is where new actions or events slipped in during tests
    const editors = new Map();
    for (const r of rows) {
      if (r.route === "rewrite") {
        if (!editors.has(r.id)) editors.set(r.id, new Set());
        editors.get(r.id).add(r.src);
      }
    }
    for (const m of merged) {
      if (m.route === "rewrite" && (editors.get(m.id)?.size ?? 0) === 1) {
        const head = [...strip(m.fix || "").replace(/^(#+|[-*+]|\p{Nd}+[.)])\s+/u, "")];
        const short = head.slice(0, 15).join("") + (head.length > 15 ? "…" : "");
        m.ask = (m.ask || "") + SINGLE_REWRITE_ASK(short);
      }
    }
  }
  // the bold check only speaks for sentences no finder run mentioned
  const mentioned = new Set(rows.map((r) => r.id));
  const all = merged.concat(boldFindings(us).filter((b) => !mentioned.has(b.id)));
  const order = new Map(us.map((u, k) => [u.id, k]));
  all.sort((a, b) => order.get(a.id) - order.get(b.id));
  return [all, warnings];
}

// 質問する and 質問します, 聞く and 聞きます compare equal: drop the trailing kana.
function alignKey(word) {
  const w = String(word || "").replace(/[「」\s]/gu, "");
  const stem = w.replace(/[ぁ-ゖ]+$/u, "");
  return stem || w;
}

// Rewrites of one ID from different runs that align toward different words are
// not an agreement: which word the writer meant is not in the text. A run's align
// word must also be in every other run's rewrite of that ID (a run that rewrote
// the sentence under another type still chose a word). On a conflict the rewrites
// are dropped and the ID gets an ask naming the words (other questions stay).
function alignConflicts(rows) {
  const warnings = [], rewrites = new Map(), words = new Map();
  for (const r of rows) {
    if (r.route !== "rewrite") continue;
    if (!rewrites.has(r.id)) rewrites.set(r.id, []);
    rewrites.get(r.id).push(r);
    if (!ALIGN_TYPES.test(r.pattern || "")) continue;
    const w = strip(String(r.align || "").replace(/[「」]/gu, ""));
    if (!w) {
      warnings.push(`run ${r.src + 1}: ${r.id} aligns a word but has no align`);
      continue;
    }
    if (!words.has(r.id)) words.set(r.id, []);
    const ws = words.get(r.id);
    if (!ws.map(alignKey).includes(alignKey(w))) ws.push(w);
  }
  const conflicts = new Map();
  for (const [i, ws] of words) {
    const rs = rewrites.get(i);
    if (new Set(rs.map((r) => r.src)).size < 2) continue;
    const missing = ws.some((w) => rs.some((r) => !(r.fix || "").includes(alignKey(w))));
    if (ws.length > 1 || missing) {
      conflicts.set(i, ws);
      warnings.push(`align conflict ${i}: ${ws.join(" / ")} -> ask`);
    }
  }
  if (!conflicts.size) return [rows, warnings];
  const out = [], asked = new Set();
  for (const r of rows) {
    if (conflicts.has(r.id) && r.route === "rewrite") {
      if (!asked.has(r.id)) {
        asked.add(r.id);
        const ws = conflicts.get(r.id);
        const q = ws.length > 1 ? ALIGN_ASK(ws.map((w) => `「${w}」`).join("・")) : ALIGN_ASK1(ws[0]);
        out.push({ id: r.id, route: "ask", fix: q, src: r.src });
      }
      continue;
    }
    out.push(r);
  }
  return [out, warnings];
}

function cmdMerge(unitsPath, outPath, mode, findingPaths) {
  const [, us] = loadUnits(unitsPath);
  const warn = [];
  const runs = findingPaths.map((p) => readJsonl(p, warn));
  const [merged, w] = merge(us, runs, mode);
  writeJsonl(outPath, merged);
  const counts = Object.fromEntries(ROUTES.map((k) => [k, merged.filter((r) => r.route === k).length]));
  console.log(dumps({ runs: runs.length, mode, routes: counts, warnings: warn.concat(w) }));
  return 0;
}

// ---------- assemble ----------

// Cause, limit and time words, and numbers, in new that the whole original never had.
const MARKERS = ADDED_MARKERS.concat(TIME_MARKERS);

function addedWords(nw, origDoc, origNums) {
  const hits = MARKERS.filter((m) => nw.includes(m) && !origDoc.includes(m));
  return hits.concat(findall(NUM, nw).filter((n) => !origNums.has(n)));
}

// Returns [body, log]. us is changed in place.
function assemble(us, merged) {
  const byId = new Map(us.map((u) => [u.id, u]));
  const origDoc = us.map((u) => u.text).join("");
  const origNums = new Set(findall(NUM, origDoc));
  const before = new Map(us.map((u) => [u.id, u.text]));
  const changes = [], reverted = [], confirm = [], skipped = [];
  for (const f of merged) {
    const u = byId.get(f.id);
    if (!u || u.raw) {
      skipped.push(f.id);
      continue;
    }
    const old = u.text;
    if (f.route === "keep") {
      changes.push({ id: f.id, route: "keep", result: "残した", pattern: get(f, "pattern", ""), before: strip(old), after: strip(old) });
      continue;
    }
    const lead = old.slice(0, old.length - lstrip(old).length);
    // trailing spaces (a Markdown hard break at the end of a line) stay where they were
    const trail = old.slice(rstrip(old).length);
    const pm = BLOCK_START.exec(lstrip(old));
    const prefix = pm ? pm[1] : "";
    let nw;
    if (f.route === "delete") {
      nw = "";
      if (f.ask) confirm.push({ id: f.id, ask: f.ask });
    } else if (f.route === "ask") {
      nw = strip(old);
      confirm.push({ id: f.id, ask: f.fix });
    } else {
      nw = strip(f.fix || "");
      const hits = addedWords(nw, origDoc, origNums);
      if (hits.length || !nw) {
        reverted.push({ id: f.id, proposed: nw, words: hits.length ? hits : ["空の言い換え"] });
        nw = strip(old);
      }
      if (f.ask) confirm.push({ id: f.id, ask: f.ask });
    }
    if (nw && prefix && !nw.startsWith(strip(prefix))) nw = prefix + nw;
    u.text = nw ? lead + nw + trail : "";
    if (u.text !== old) {
      changes.push({ id: f.id, route: f.route, result: "変えた", pattern: get(f, "pattern", ""), before: strip(old), after: strip(u.text) });
    }
  }

  // The body carries only a short mark after the sentence; the questions go to the
  // confirmation list. A question about a deleted sentence is marked on the nearest
  // earlier sentence that remains, numbers follow the order in the body, and the
  // same question asked twice gets one number.
  const order = new Map(us.map((u, k) => [u.id, k]));
  const markable = (x) => {
    const t = strip(x.text);
    return Boolean(t) && !x.raw && !t.startsWith("#") && !URL_LINE.test(t);
  };
  const anchor = (i) => {
    // a question about a heading goes on the first sentence of its own section;
    // when the next heading comes first, on the heading itself. Never on the
    // section before or after
    const k = order.get(i);
    if (strip(us[k].text).startsWith("#")) {
      for (const x of us.slice(k + 1)) {
        if (strip(x.text).startsWith("#")) break;
        if (markable(x)) return x.id;
      }
      return i;
    }
    for (let j = k; j >= 0; j--) if (markable(us[j])) return us[j].id;
    return us.slice(k).find(markable)?.id ?? i;
  };
  for (const c of confirm) {
    c.at = anchor(c.id);
    // the list names the heading, since its mark may sit on the sentence under it
    const h = strip(byId.get(c.id).text);
    if (h.startsWith("#")) {
      const cs = [...h.replace(/^#+\s*/u, "")];
      c.heading = cs.slice(0, 20).join("") + (cs.length > 20 ? "…" : "");
    }
  }
  confirm.sort((a, b) => order.get(a.at) - order.get(b.at) || order.get(a.id) - order.get(b.id));
  // a question already listed is not listed again; a finding whose questions
  // were all listed before gets no mark of its own
  const unique = [], seen = new Set();
  for (const c of confirm) {
    const qs = splitAsks(c.ask).filter((q) => !seen.has(q));
    if (!qs.length) continue;
    qs.forEach((q) => seen.add(q));
    c.questions = qs;
    unique.push(c);
  }
  const dangling = danglingRefs(us, before);
  const orphans = orphanParagraphs(us, before);
  unique.forEach((c, k) => {
    c.no = k + 1;
    addMark(byId.get(c.at), `〔確認${k + 1}〕`);
  });
  let body = render(us);
  const notes = dangling.map(danglingLine).concat(orphans.map(orphanLine));
  if (unique.length || notes.length) {
    body += "\n---\n\n## 確認欄\n\n" + unique.map(confirmLines).join("") + notes.join("");
  }
  const edited = changes.filter((c) => c.route !== "keep");
  const log = {
    changes, reverted, confirm: unique, dangling, orphans,
    skipped: skipped.filter((s) => s), unchanged_ids: before.size - edited.length,
  };
  return [body, log];
}

// Put the mark right after the sentence's end mark, before any trailing spaces.
function addMark(u, mark) {
  const t = u.text;
  const end = rstrip(t).length;
  u.text = t.slice(0, end) + mark + t.slice(end);
}

function deletedIds(us, before) {
  return us.filter((u) => before.get(u.id) && strip(before.get(u.id)) && !u.text).map((u) => u.id);
}

// Words that appear only in a deleted sentence, while a later remaining sentence
// points at them; see dangling_refs in japango.py.
function danglingRefs(us, before) {
  const order = new Map(us.map((u, k) => [u.id, k]));
  const remaining = us.map((u) => u.text).join("");
  const out = [];
  for (const i of deletedIds(us, before)) {
    const words = [];
    for (const w of findall(NOUN, unbold(before.get(i)))) {
      if (!remaining.includes(w) && !words.includes(w)) words.push(w);
    }
    if (!words.length) continue;
    for (const u of us.slice(order.get(i) + 1)) {
      if (!u.text || u.raw) continue;
      for (const m of u.text.matchAll(POINTER)) {
        const head = m[1];
        const hits = words.filter((w) => w.includes(head));
        if (hits.length) out.push({ deleted: i, words: hits, at: u.id, ref: m[0] });
      }
    }
  }
  return out;
}

function danglingLine(d) {
  return `- 削った文が指していた語が残っていません: ${d.words.join("・")}` +
    `（${d.at} の「${d.ref}」が受けている。${d.deleted} を削った）\n`;
}

// Prose paragraphs that had two or more sentences and keep exactly one after
// deletions; see orphan_paragraphs in japango.py.
function orphanParagraphs(us, before) {
  const paras = new Map();
  for (const u of us) {
    if (!paras.has(u.para)) paras.set(u.para, []);
    paras.get(u.para).push(u);
  }
  const out = [];
  if ([...paras.values()].filter((xs) => xs.some((x) => strip(x.text))).length < 2) return out;
  for (const [p, xs] of paras) {
    if (xs.some((x) => x.raw || BLOCK_START.test(lstrip(before.get(x.id))))) continue;
    const had = xs.filter((x) => strip(before.get(x.id)));
    const left = xs.filter((x) => strip(x.text));
    if (had.length >= 2 && left.length === 1) {
      out.push({ para: p, id: left[0].id, text: strip(left[0].text), deleted: had.filter((x) => !strip(x.text)).map((x) => x.id) });
    }
  }
  return out;
}

const orphanLine = (o) => `- 1 文だけ残った段落: 〔${o.text}〕（${o.id}）\n`;

// One question on the same line as its mark; several, one per line under it.
function confirmLines(c) {
  const qs = c.questions;
  const head = c.heading ? `（見出し「${c.heading}」）` : "";
  if (qs.length === 1) return `- 〔確認${c.no}〕${head}${qs[0]}\n`;
  return `- 〔確認${c.no}〕${head}\n` + qs.map((q) => `  - ${q}\n`).join("");
}

// <stem>.japango.md next to source, numbered when the name is taken.
function outputPath(source) {
  const dir = path.dirname(source), stem = stemOf(source);
  for (let n = 1; ; n++) {
    const cand = path.join(dir, `${stem}.japango${n === 1 ? "" : `-${n}`}.md`);
    if (!fs.existsSync(cand) && resolve(cand) !== resolve(source)) return cand;
  }
}

function cmdAssemble(unitsPath, mergedPath, logPath, outPath = null) {
  const source = loadMeta(unitsPath).source;
  if (outPath === null) {
    if (!source) {
      console.error("assemble: the units file has no source path; give --out");
      return 2;
    }
    outPath = outputPath(source);
  } else if (source && resolve(outPath) === resolve(source)) {
    console.error("assemble: --out is the source itself; the source is never overwritten");
    return 2;
  }
  const [front, us] = loadUnits(unitsPath);
  const [body, log] = assemble(us, readJsonl(mergedPath));
  writeText(outPath, front + body);
  writeText(logPath, dumps(log, 1) + "\n");
  const kept = log.changes.filter((c) => c.route === "keep").length;
  console.log(dumps({
    changed: log.changes.length - kept, kept, reverted: log.reverted.length, confirm: log.confirm.length,
    dangling: log.dangling.length, orphans: log.orphans.length, out: outPath,
  }));
  return 0;
}

const USAGE = "usage: japango.mjs {ids SRC WORKDIR | merge UNITS OUT {agree,union} FINDINGS... | " +
  "assemble UNITS MERGED LOG_JSON [--out OUT_MD]}";

function main(argv) {
  const fail = (msg) => {
    console.error(USAGE + "\njapango.mjs: error: " + msg);
    return 2;
  };
  let out = null;
  const args = [];
  for (let k = 0; k < argv.length; k++) {
    if (argv[k] === "--out") {
      if (k + 1 >= argv.length) return fail("--out needs a path");
      out = argv[++k];
    } else if (argv[k].startsWith("--out=")) {
      out = argv[k].slice(6);
    } else {
      args.push(argv[k]);
    }
  }
  const [cmd, ...rest] = args;
  if (cmd === "ids" && rest.length === 2 && out === null) return cmdIds(...rest);
  if (cmd === "merge" && rest.length >= 4 && out === null) {
    if (!["agree", "union"].includes(rest[2])) return fail(`invalid mode ${rest[2]}`);
    return cmdMerge(rest[0], rest[1], rest[2], rest.slice(3));
  }
  if (cmd === "assemble" && rest.length === 3) return cmdAssemble(rest[0], rest[1], rest[2], out);
  return fail("wrong arguments");
}

process.exitCode = main(process.argv.slice(2));
