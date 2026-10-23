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

// the mark assemble puts after a sentence, and the confirmation list it writes at the
// end; see MARK and CONFIRM_ITEM in japango.py
const MARK = /〔確認[0-9]+〕/gu;
const MARK_NO = /〔確認([0-9]+)〕/gu;
const LEAD_MARKS = /^(?:〔確認[0-9]+〕)+/u;
const CONFIRM_HEAD = "---\n\n## 確認欄\n\n";
const CONFIRM_ITEM = /^(?:- 〔確認[0-9]+〕|  - |- 削った文が指していた語が残っていません: |- 1 文だけ残った段落: |- 言い直しとして削った文にしか無い語: )/u;
// a deletion as a restatement (型 6); see RESTATE in japango.py
const RESTATE = /^\s*型\s*6(?![0-9a-z])/u;
const unmark = (s) => s.replace(MARK, "");

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

// [body before the confirmation list, the list] when the body ends with a list
// in the form assemble writes; [body, null] otherwise.
function splitConfirm(body) {
  let k = body.lastIndexOf("\n\n" + CONFIRM_HEAD), start;
  if (k !== -1) start = k + 2;
  else if (body.startsWith(CONFIRM_HEAD)) k = start = 0;
  else return [body, null];
  const tail = stripChars(body.slice(start), "\n", false, true);
  const items = tail.split("\n").slice(4);
  if (!items.length || !items.every((l) => CONFIRM_ITEM.test(l))) return [body, null];
  return [body.slice(0, k), tail];
}

// Units of the body. Each has id, text, para, line (one unit per line)
// and raw (kept as is, not shown to the finder). An earlier assemble's
// confirmation list at the end is one raw unit with confirm.
function units(text) {
  const out = [];
  const [body, box] = splitConfirm(splitFront(text)[1]);
  const blocks = blocksOf(body);
  blocks.forEach(([gap, lines, code], k) => {
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
  if (box !== null) {
    const pi = blocks.length + 1;
    out.push({ para: pi, gap: blocks.length ? "\n\n" : "\n", id: `p${pi}-s1`, text: box, line: true, raw: true, confirm: true });
  }
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

const withoutJoin = (r) => Object.fromEntries(Object.entries(r).filter(([k]) => k !== "join"));
const overlaps = (a, b) => (a || []).some((x) => b.includes(x));
const sameList = (a, b) => a.length === b.length && a.every((x, k) => x === b[k]);

// [span, error] for a rewrite's "join"; see join_error in japango.py.
function joinError(r, us, pos) {
  const j = get(r, "join", null);
  if (j === null || (Array.isArray(j) && (j.length === 0 || (j.length === 1 && j[0] === r.id)))) return [null, null];
  if (!Array.isArray(j) || j.length < 2 || j[0] !== r.id || !j.every((x) => typeof x === "string" && pos.has(x))) {
    return [null, "join not contiguous"];
  }
  const ks = j.map((x) => pos.get(x));
  const xs = ks.map((k) => us[k]);
  if (ks.some((k, n) => k !== ks[0] + n) || new Set(xs.map((x) => x.para)).size > 1) return [null, "join not contiguous"];
  if (xs.some((x) => x.raw || x.line)) return [null, "join over lines"];
  // a sentence that is only an earlier round's 〔確認n〕 mark: joined, the mark would go
  if (xs.some((x) => unmark(x.text) !== x.text && !strip(unmark(x.text)))) return [null, "join over mark"];
  return [[...j], null];
}

// Pick the joins to use; see resolve_joins in japango.py.
function resolveJoins(rows, mode, runs) {
  const warnings = [], accepted = [];
  const kept = new Set(rows.filter((r) => r.route === "keep").map((r) => r.id));
  const touches = (x, span) => span.includes(x.id) || overlaps(x.join, span);
  const cands = rows.filter((r) => r.join).sort((a, b) => a.src - b.src);
  for (const r of cands) {
    const span = r.join;
    const clash = accepted.find((a) => overlaps(a.join, span));
    if (clash) {
      if (!sameList(clash.join, span)) warnings.push(`run ${r.src + 1}: join span differs ${r.id}`);
      continue;
    }
    if (span.some((x) => kept.has(x))) {
      warnings.push(`run ${r.src + 1}: join kept ${r.id}`);
      continue;
    }
    if (mode === "agree") {
      const editors = new Set(rows.filter((x) => EDIT_ROUTES.includes(x.route) && touches(x, span)).map((x) => x.src));
      if (editors.size < runs) continue;
    }
    accepted.push(r);
  }
  const inside = new Map();
  for (const a of accepted) for (const j of a.join) inside.set(j, a.id);
  const rest = [];
  for (const r of rows) {
    if (r.join) continue;
    if (EDIT_ROUTES.includes(r.route) && inside.has(r.id)) {
      if (mode === "union") warnings.push(`run ${r.src + 1}: join ${inside.get(r.id)} drops ${r.route} ${r.id}`);
      continue;
    }
    rest.push(r);
  }
  return [accepted, rest, warnings];
}

// runs: one list of findings per finder run. Returns [merged, warnings].
function merge(us, runs, mode = "agree") {
  const ids = new Set(us.filter((u) => !u.raw).map((u) => u.id));
  const pos = new Map(us.map((u, k) => [u.id, k]));
  const warnings = [], rows = [];
  runs.forEach((run, n) => {
    const seen = new Set();
    for (let r of run) {
      const i = r.id, route = r.route;
      if (typeof i !== "string" || !ids.has(i) || !ROUTES.includes(route)) {
        warnings.push(`run ${n + 1}: skipped ${pyRepr(i)} (route ${pyRepr(route)})`);
        continue;
      }
      if (route === "rewrite" && "join" in r) {
        const [span, err] = joinError(r, us, pos);
        if (err) {
          warnings.push(`run ${n + 1}: ${err} ${i}`);
          continue;
        }
        r = span ? { ...r, join: span } : withoutJoin(r);
      } else if ("join" in r) {
        r = withoutJoin(r);
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
  const [joins, rest, wj] = resolveJoins(aligned, mode, runs.length);
  warnings.push(...wj);
  const merged = mode === "agree" ? mergeFindingsAgree(rest, runs.length) : mergeFindings(rest);
  // a join takes the place of its first sentence's row; that sentence's questions stay on it
  for (const a of joins) {
    const k = merged.findIndex((m) => m.id === a.id);
    const head = k >= 0 ? merged[k] : null;
    const asks = head && head.route === "ask" ? head.fix : "";
    if (head) merged.splice(k, 1);
    merged.push({ id: a.id, route: "rewrite", fix: get(a, "fix", ""), ask: asks, pattern: get(a, "pattern", ""), join: a.join });
  }
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
      const nEditors = m.join
        ? new Set(aligned.filter((r) => r.route === "rewrite" && (m.join.includes(r.id) || overlaps(r.join, m.join))).map((r) => r.src)).size
        : (editors.get(m.id)?.size ?? 0);
      if (m.route === "rewrite" && nEditors === 1) {
        const head = [...strip(m.fix || "").replace(/^(#+|[-*+]|\p{Nd}+[.)])\s+/u, "")];
        const short = head.slice(0, 15).join("") + (head.length > 15 ? "…" : "");
        m.ask = (m.ask || "") + SINGLE_REWRITE_ASK(short);
      }
    }
  }
  // the bold check only speaks for sentences no finder run mentioned
  const mentioned = new Set(rows.map((r) => r.id));
  for (const r of aligned) for (const j of r.join || []) mentioned.add(j);
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
  // 「まま」is not a time word: 「書式のままです」 holds 「まで」 only across the two words
  const probe = nw.replaceAll("まま", "＿＿");
  const src = origDoc.replaceAll("まま", "＿＿");
  const hits = MARKERS.filter((m) => probe.includes(m) && !src.includes(m));
  return hits.concat(findall(NUM, nw).filter((n) => !origNums.has(n)));
}

// Returns [body, log]. us is changed in place.
function assemble(us, merged) {
  const byId = new Map(us.map((u) => [u.id, u]));
  const pos = new Map(us.map((u, k) => [u.id, k]));
  // the original to compare against: without an earlier round's confirmation list and marks
  const box = us.find((u) => u.confirm) ?? null;
  const origDoc = unmark(us.filter((u) => !u.confirm).map((u) => u.text).join(""));
  const origNums = new Set(findall(NUM, origDoc));
  const firstNo = Math.max(0, ...us.flatMap((u) => [...u.text.matchAll(MARK_NO)].map((m) => parseInt(m[1], 10))));
  const before = new Map(us.map((u) => [u.id, u.text]));
  const changes = [], reverted = [], confirm = [], skipped = [];
  const joined = new Set(); // sentences a join folded into the sentence before them
  for (const f of merged) {
    const u = byId.get(f.id);
    let [span, err] = f.route === "rewrite" ? joinError(f, us, pos) : [null, null];
    if (!u || u.raw || err || (joined.has(f.id) && f.route !== "ask")) {
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
      const bare = unmark(nw);
      const hits = addedWords(bare, origDoc, origNums);
      if (hits.length || !strip(bare)) {
        // a join that is put back puts back every sentence in it
        reverted.push({ id: f.id, proposed: nw, words: hits.length ? hits : ["空の言い換え"], ...(span ? { join: span } : {}) });
        nw = strip(old);
        span = null;
      }
      if (f.ask) confirm.push({ id: f.id, ask: f.ask });
    }
    if (nw && prefix && !nw.startsWith(strip(prefix))) nw = prefix + nw;
    if (f.route === "rewrite") nw = keepMarks((span || [f.id]).map((j) => byId.get(j).text), nw);
    if (span) {
      // the rest of the join goes as a deleted sentence does
      const oldSpan = span.map((j) => byId.get(j).text).join("");
      for (const j of span.slice(1)) {
        byId.get(j).text = "";
        joined.add(j);
      }
      u.text = lead + nw + trail;
      changes.push({ id: f.id, route: f.route, result: "変えた", pattern: get(f, "pattern", ""), before: strip(oldSpan), after: strip(u.text), join: span });
      continue;
    }
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
  const dangling = danglingRefs(us, before, joined);
  const orphans = orphanParagraphs(us, before, joined);
  const lost = lostWords(us, before, joined, changes, dangling);
  // numbers go on from the highest mark already in the text (an earlier round's)
  unique.forEach((c, k) => {
    c.no = firstNo + k + 1;
    addMark(byId.get(c.at), `〔確認${c.no}〕`);
  });
  const notes = dangling.map(danglingLine).concat(lost.map(lostLine), orphans.map(orphanLine));
  const lines = unique.map(confirmLines).join("") + notes.join("");
  // an earlier round's list takes the new lines at its end: one list, one heading
  if (box !== null && lines) box.text += "\n" + stripChars(lines, "\n", false, true);
  let body = render(us);
  if (box === null && lines) body += "\n---\n\n## 確認欄\n\n" + lines;
  const edited = changes.filter((c) => c.route !== "keep");
  const log = {
    changes, reverted, confirm: unique, dangling, lost, orphans,
    skipped: skipped.filter((s) => s), unchanged_ids: before.size - edited.reduce((s, c) => s + (c.join ? c.join.length : 1), 0),
  };
  return [body, log];
}

// An earlier round's marks the rewrite left out go back; see keep_marks in japango.py.
function keepMarks(olds, nw) {
  const m = LEAD_MARKS.exec(strip(olds[0]));
  const lead = m ? findall(MARK, m[0]) : [];
  const front = lead.filter((x) => !nw.includes(x));
  const back = findall(MARK, olds.join("")).filter((x) => !nw.includes(x) && !lead.includes(x));
  return front.join("") + nw + back.join("");
}

// Put the mark right after the sentence's end mark, before any trailing spaces.
function addMark(u, mark) {
  const t = u.text;
  const end = rstrip(t).length;
  u.text = t.slice(0, end) + mark + t.slice(end);
}

// a sentence a join folded away was rewritten, not deleted
function deletedIds(us, before, joined = new Set()) {
  return us.filter((u) => before.get(u.id) && strip(before.get(u.id)) && !u.text && !joined.has(u.id)).map((u) => u.id);
}

// Words that appear only in a deleted sentence, while a later remaining sentence
// points at them; see dangling_refs in japango.py.
function danglingRefs(us, before, joined = new Set()) {
  const order = new Map(us.map((u, k) => [u.id, k]));
  const out = [];
  for (const i of deletedIds(us, before, joined)) {
    const words = onlyWords(us, before, i);
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

// Nouns of the deleted sentence i found nowhere in the remaining body.
function onlyWords(us, before, i) {
  const remaining = us.filter((u) => !u.confirm).map((u) => u.text).join("");
  const words = [];
  for (const w of findall(NOUN, unbold(unmark(before.get(i))))) {
    if (!remaining.includes(w) && !words.includes(w)) words.push(w);
  }
  return words;
}

// Sentences deleted as a restatement (型 6) that held a word found nowhere else in
// the body; see lost_words in japango.py.
function lostWords(us, before, joined, changes, dangling = []) {
  const restate = new Set(changes.filter((c) => c.route === "delete" && RESTATE.test(c.pattern || "")).map((c) => c.id));
  const out = [];
  for (const i of deletedIds(us, before, joined)) {
    if (!restate.has(i)) continue;
    const told = new Set(dangling.filter((d) => d.deleted === i).flatMap((d) => d.words));
    const words = onlyWords(us, before, i).filter((w) => !told.has(w));
    if (words.length) out.push({ deleted: i, words, text: strip(before.get(i)) });
  }
  return out;
}

const lostLine = (x) => `- 言い直しとして削った文にしか無い語: ${x.words.join("・")}（${x.deleted}「${x.text}」）\n`;

// Prose paragraphs that had two or more sentences and keep exactly one after
// deletions; see orphan_paragraphs in japango.py.
function orphanParagraphs(us, before, joined = new Set()) {
  const paras = new Map();
  for (const u of us) {
    if (!paras.has(u.para)) paras.set(u.para, []);
    paras.get(u.para).push(u);
  }
  const out = [];
  if ([...paras.values()].filter((xs) => xs.some((x) => strip(x.text) && !x.confirm)).length < 2) return out;
  for (const [p, xs] of paras) {
    if (xs.some((x) => x.raw || BLOCK_START.test(lstrip(before.get(x.id))))) continue;
    const had = xs.filter((x) => strip(before.get(x.id)) && !joined.has(x.id));
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
    dangling: log.dangling.length, lost: log.lost.length, orphans: log.orphans.length, out: outPath,
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
