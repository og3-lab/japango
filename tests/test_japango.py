import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "japango" / "scripts"))

import japango as jp  # noqa: E402

WORK = ROOT / ".work"


def tmpdir():
    """Temporary folders go under the repository's .work/ (ignored by git)."""
    WORK.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(dir=WORK))


def run_cli(args):
    """(exit code, the JSON the command printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = jp.main(args)
    return code, json.loads(out.getvalue()) if out.getvalue().strip() else None

DOC = """---
title: 例
---

# 見出し

最初の文です。  二つ目の文です！**三つ目は太字。**

- 項目の一つ目。
- 項目の二つ目。

```
コードの中。改行も。

空行も残る。
```

| 表 | 値 |
|---|---|


最後の段落は 15 分で終わりました。理由は書いていません
"""


def unit(us, i):
    return next(u for u in us if u["id"] == i)


class RoundTrip(unittest.TestCase):
    def test_render_restores_body(self):
        us = jp.units(DOC)
        self.assertEqual(jp.split_front(DOC)[0] + jp.render(us), DOC)

    def test_eval_materials_round_trip(self):
        files = sorted((ROOT / "evals").glob("*/*.md"))
        self.assertTrue(files)
        for f in files:
            text = jp.read_text(f)
            self.assertEqual(jp.render(jp.units(text)), jp.expected_body(text), f.name)

    def test_ids(self):
        us = jp.units(DOC)
        self.assertEqual(unit(us, "p2-s2")["text"], "  二つ目の文です！")
        self.assertEqual(unit(us, "p2-s3")["text"], "**三つ目は太字。**")
        self.assertTrue(unit(us, "p3-s2")["line"])
        self.assertTrue(unit(us, "p4-s1").get("raw"))
        self.assertTrue(unit(us, "p5-s1").get("raw"))

    def test_cli_ids_in_workdir(self):
        tmp = tmpdir()
        try:
            src = tmp / "doc.md"
            jp.write_text(src, DOC)
            self.assertEqual(jp.main(["ids", str(src), str(tmp / "w")]), 0)
            annotated = (tmp / "w" / "doc.annotated.txt").read_text(encoding="utf-8")
            self.assertIn("[p2-s1] 最初の文です。", annotated)
            self.assertNotIn("コードの中", annotated)
            self.assertEqual((tmp / "w" / "doc.orig.md").read_text(encoding="utf-8"), DOC)
        finally:
            shutil.rmtree(tmp)


class Merge(unittest.TestCase):
    us = [{"id": f"p1-s{i}", "text": f"文{i}。", "para": 1} for i in range(1, 6)]

    def test_agree_needs_every_run(self):
        a = [{"id": "p1-s1", "route": "delete"}, {"id": "p1-s2", "route": "rewrite", "fix": "新しい文。"}]
        b = [{"id": "p1-s1", "route": "delete"}]
        merged, _ = jp.merge(self.us, [a, b], "agree")
        self.assertEqual([(r["id"], r["route"]) for r in merged], [("p1-s1", "delete")])

    def test_union_takes_either(self):
        a = [{"id": "p1-s2", "route": "rewrite", "fix": "新しい文。"}]
        merged, _ = jp.merge(self.us, [a, []], "union")
        self.assertEqual(merged[0]["route"], "rewrite")

    def test_union_single_rewrite_is_marked(self):
        a = [{"id": "p1-s2", "route": "rewrite", "fix": "新しい文。"},
             {"id": "p1-s3", "route": "rewrite", "fix": "別の文。"},
             {"id": "p1-s4", "route": "rewrite", "fix": "両方の文。"}]
        b = [{"id": "p1-s4", "route": "rewrite", "fix": "両方の文。"}]
        merged, _ = jp.merge(self.us, [a, b], "union")
        asks = {r["id"]: r.get("ask", "") for r in merged}
        self.assertIn("新しい文", asks["p1-s2"])
        self.assertIn("別の文", asks["p1-s3"])
        self.assertEqual(asks["p1-s4"], "")
        body, log = jp.assemble([dict(u) for u in self.us], merged)
        self.assertIn("〔確認1〕", body)
        self.assertIn("〔確認2〕", body)
        merged, _ = jp.merge(self.us, [a, b], "agree")
        self.assertTrue(all(not r.get("ask") for r in merged))

    def test_single_rewrite_of_heading_marks_next_sentence(self):
        us = [{"id": "p1-s1", "text": "前の節の文。", "para": 1},
              {"id": "p2-s1", "text": "## 見出し", "para": 2},
              {"id": "p3-s1", "text": "見出しの下の文。", "para": 3}]
        a = [{"id": "p2-s1", "route": "rewrite", "fix": "## 新しい見出し"}]
        merged, _ = jp.merge(us, [a, []], "union")
        self.assertNotIn("#", merged[0]["ask"])
        body, _ = jp.assemble([dict(u) for u in us], merged)
        self.assertIn("見出しの下の文。〔確認1〕", body)
        self.assertNotIn("前の節の文。〔確認1〕", body)

    def test_heading_ask_stays_in_its_section(self):
        # the next unit is another heading, then a bare URL: the mark goes on the
        # heading itself, not after the URL of the next section
        us = [{"id": "p1-s1", "text": "# 題", "para": 1},
              {"id": "p2-s1", "text": "## 動画", "para": 2},
              {"id": "p3-s1", "text": "https://example.com/v", "para": 3},
              {"id": "p4-s1", "text": "本文の文。", "para": 4}]
        a = [{"id": "p1-s1", "route": "ask", "fix": "〔どのように作ったか〕"}]
        merged, _ = jp.merge(us, [a, a], "agree")
        body, log = jp.assemble([dict(u) for u in us], merged)
        self.assertIn("# 題〔確認1〕", body)
        self.assertIn("https://example.com/v\n", body)
        self.assertNotIn("本文の文。〔確認1〕", body)
        self.assertIn("- 〔確認1〕（見出し「題」）どのように作ったか", body)
        self.assertEqual(log["confirm"][0]["at"], "p1-s1")

    def test_heading_ask_with_sentence_names_the_heading(self):
        us = [{"id": "p1-s1", "text": "## 見出し", "para": 1},
              {"id": "p2-s1", "text": "見出しの下の文。", "para": 2}]
        a = [{"id": "p1-s1", "route": "ask", "fix": "〔何を指すか〕"}]
        merged, _ = jp.merge(us, [a, a], "agree")
        body, _ = jp.assemble([dict(u) for u in us], merged)
        self.assertIn("見出しの下の文。〔確認1〕", body)
        self.assertIn("- 〔確認1〕（見出し「見出し」）何を指すか", body)

    def test_align_conflict_becomes_ask(self):
        # both runs rewrite, but toward different words: not an agreement
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21e 同じ動作を別の動詞で呼ぶ",
              "align": "質問する", "fix": "あなたに質問します。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21e", "align": "聞く",
              "fix": "あなたに聞きます。"}]
        for mode in ("agree", "union"):
            merged, warnings = jp.merge(self.us, [a, b], mode)
            row = next(r for r in merged if r["id"] == "p1-s2")
            self.assertEqual(row["route"], "ask", mode)
            self.assertEqual(row["fix"], "〔「質問する」・「聞く」のどれにそろえるか〕")
            self.assertTrue(any("align conflict p1-s2" in w for w in warnings))
        body, _ = jp.assemble([dict(u) for u in self.us], merged)
        self.assertNotIn("あなたに", body)
        self.assertIn("〔確認1〕", body)

    def test_align_same_word_in_other_form_agrees(self):
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21d", "align": "質問する",
              "fix": "担当者に質問します。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21d", "align": "「質問します」",
              "fix": "担当者へ質問します。"}]
        merged, warnings = jp.merge(self.us, [a, b], "agree")
        self.assertEqual(merged[0]["route"], "rewrite")
        self.assertEqual(merged[0]["fix"], "担当者に質問します。")
        self.assertEqual(warnings, [])

    def test_align_word_missing_from_other_run_is_a_conflict(self):
        # run 2 rewrote the same sentence under another type and chose 聞く: the
        # agreement on rewrite is not an agreement on the word
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型32／型21e", "align": "質問する",
              "fix": "あなたに質問する、を決めます。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型32 原稿の中の食い違い",
              "fix": "あなたに聞く、を決めます。"}]
        merged, _ = jp.merge(self.us, [a, b], "agree")
        row = next(r for r in merged if r["id"] == "p1-s2")
        self.assertEqual(row["route"], "ask")
        self.assertEqual(row["fix"], "〔「質問する」とほかの回の言い方のどちらにそろえるか〕")

    def test_align_only_for_types_21(self):
        # other types and 21b/21c never compare align; a missing align is reported
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21c", "align": "x", "fix": "一。"},
             {"id": "p1-s3", "route": "rewrite", "pattern": "型21e", "fix": "二。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21c", "align": "y", "fix": "一。"},
             {"id": "p1-s3", "route": "rewrite", "pattern": "型21e", "fix": "二。"}]
        merged, warnings = jp.merge(self.us, [a, b], "agree")
        self.assertEqual([r["route"] for r in merged], ["rewrite", "rewrite"])
        self.assertEqual(len([w for w in warnings if "has no align" in w]), 2)

    def test_keep_wins_and_asks_are_union(self):
        a = [{"id": "p1-s1", "route": "delete"}, {"id": "p1-s3", "route": "ask", "fix": "〔誰が決めたか〕"}]
        b = [{"id": "p1-s1", "route": "keep"}]
        for mode in ("agree", "union"):
            merged, _ = jp.merge(self.us, [a, b], mode)
            routes = {r["id"]: r["route"] for r in merged}
            self.assertNotEqual(routes.get("p1-s1"), "delete", mode)
            self.assertEqual(routes["p1-s3"], "ask", mode)

    def test_deadline_only_ask_dropped_and_unknown_id_warned(self):
        a = [{"id": "p1-s1", "route": "ask", "fix": "〔いつまでに直すか〕"},
             {"id": "p9-s9", "route": "delete"}]
        merged, warnings = jp.merge(self.us, [a], "agree")
        self.assertEqual(merged, [])
        self.assertEqual(len(warnings), 1)

    def test_keep_twice_stays_as_keep_row(self):
        a = [{"id": "p1-s1", "route": "keep", "pattern": "型17"}, {"id": "p1-s2", "route": "delete"}]
        b = [{"id": "p1-s1", "route": "keep"}, {"id": "p1-s2", "route": "keep"}]
        for mode in ("agree", "union"):
            merged, _ = jp.merge(self.us, [a, b], mode)
            self.assertEqual([(r["id"], r["route"]) for r in merged],
                             [("p1-s1", "keep"), ("p1-s2", "keep")], mode)

    def test_keep_with_question_becomes_ask(self):
        a = [{"id": "p1-s1", "route": "keep"}]
        b = [{"id": "p1-s1", "route": "ask", "fix": "〔根拠は何か〕"}]
        merged, _ = jp.merge(self.us, [a, b], "agree")
        self.assertEqual([(r["route"], r["fix"]) for r in merged], [("ask", "〔根拠は何か〕")])

    def test_two_questions_kept_and_deadline_one_dropped(self):
        a = [{"id": "p1-s1", "route": "ask", "fix": "〔誰が判断したか〕〔いつまでに直すか〕〔根拠は何か〕"}]
        merged, _ = jp.merge(self.us, [a], "agree")
        self.assertEqual(merged[0]["fix"], "〔誰が判断したか〕〔根拠は何か〕")

    def test_cli_counts_keep_in_merge_and_log(self):
        tmp = tmpdir()
        try:
            src = tmp / "d.md"
            jp.write_text(src, "残す文。次の文。\n")
            run_cli(["ids", str(src), str(tmp)])
            for n in (1, 2):
                jp.write_text(tmp / f"f{n}.jsonl", json.dumps({"id": "p1-s1", "route": "keep"}) + "\n")
            _, out = run_cli(["merge", str(tmp / "d.units.jsonl"), str(tmp / "m.jsonl"), "agree",
                              str(tmp / "f1.jsonl"), str(tmp / "f2.jsonl")])
            self.assertEqual(out["routes"]["keep"], 1)
            _, out = run_cli(["assemble", str(tmp / "d.units.jsonl"), str(tmp / "m.jsonl"), str(tmp / "log.json")])
            self.assertEqual((out["changed"], out["kept"]), (0, 1))
            log = json.loads((tmp / "log.json").read_text(encoding="utf-8"))
            self.assertEqual([(c["id"], c["result"]) for c in log["changes"]], [("p1-s1", "残した")])
        finally:
            shutil.rmtree(tmp)

    def test_bold_found_mechanically(self):
        us = [{"id": "p1-s1", "text": "結果は**12 秒**から**3 秒**になった。", "para": 1}]
        merged, _ = jp.merge(us, [[]], "agree")
        self.assertEqual(merged[0]["fix"], "結果は 12 秒から 3 秒になった。")


class Assemble(unittest.TestCase):
    def build(self, text, findings):
        us = jp.units(text)
        return jp.assemble(us, findings)

    def test_confirm_numbers_follow_body_order_and_merge_duplicates(self):
        text = "一つ目。二つ目。三つ目。\n\n四つ目。\n"
        body, log = self.build(text, [
            {"id": "p2-s1", "route": "ask", "fix": "〔誰が決めたか〕"},
            {"id": "p1-s3", "route": "delete", "ask": "〔根拠は何か〕"},
            {"id": "p1-s1", "route": "ask", "fix": "〔誰が決めたか〕"},
        ])
        # the deleted sentence's question sits on the sentence before it
        self.assertIn("一つ目。〔確認1〕二つ目。〔確認2〕", body)
        self.assertNotIn("三つ目", body.split("---")[0])
        self.assertIn("四つ目。\n", body)
        self.assertIn("- 〔確認1〕誰が決めたか\n- 〔確認2〕根拠は何か\n", body)
        self.assertEqual(len(log["confirm"]), 2)

    def test_new_number_reverts(self):
        text = "待ち時間は 1 時間 25 分ほどだった。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "rewrite", "fix": "待ち時間は 85 分だった。"}])
        self.assertEqual(body, text)
        self.assertEqual(log["reverted"][0]["words"], ["85"])

    def test_new_cause_word_reverts_but_existing_word_passes(self):
        text = "警告は誰も見なかった。メールが多いため埋もれた。\n"
        body, log = self.build(text, [
            {"id": "p1-s1", "route": "rewrite", "fix": "警告は担当者だけが受け取った。"},
            {"id": "p1-s2", "route": "rewrite", "fix": "メールが多いため、警告は埋もれた。"},
        ])
        self.assertEqual([r["id"] for r in log["reverted"]], ["p1-s1"])
        self.assertIn("警告は誰も見なかった。メールが多いため、警告は埋もれた。", body)

    def test_list_marker_and_heading_kept(self):
        text = "## 結論：まとめ\n\n- 対策を進める。\n- 全力で取り組みます。\n"
        body, _ = self.build(text, [
            {"id": "p1-s1", "route": "rewrite", "fix": "まとめ"},
            {"id": "p2-s1", "route": "rewrite", "fix": "対策を進めます。"},
            {"id": "p2-s2", "route": "delete"},
        ])
        self.assertEqual(body, "## まとめ\n\n- 対策を進めます。\n")

    def test_cli_assemble_writes_next_to_source_and_numbers(self):
        tmp = tmpdir()
        try:
            src = tmp / "d.md"
            jp.write_text(src, "残す文。消す文。\n")
            work = tmp / "d.japango"
            run_cli(["ids", str(src), str(work)])
            jp.write_text(work / "f.jsonl", json.dumps({"id": "p1-s2", "route": "delete"}) + "\n")
            run_cli(["merge", str(work / "d.units.jsonl"), str(work / "m.jsonl"), "agree", str(work / "f.jsonl")])
            args = ["assemble", str(work / "d.units.jsonl"), str(work / "m.jsonl"), str(work / "log.json")]
            code, out = run_cli(args)
            self.assertEqual(code, 0)
            self.assertEqual(Path(out["out"]), tmp / "d.japango.md")
            self.assertEqual((tmp / "d.japango.md").read_text(encoding="utf-8"), "残す文。\n")
            log = json.loads((work / "log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["changes"][0]["before"], "消す文。")
            # a second run does not overwrite the first output, and the source stays as it was
            self.assertEqual(run_cli(args)[0], 0)
            self.assertEqual((tmp / "d.japango-2.md").read_text(encoding="utf-8"), "残す文。\n")
            self.assertEqual(src.read_text(encoding="utf-8"), "残す文。消す文。\n")
            # --out writes elsewhere, but never onto the source
            self.assertEqual(run_cli(args + ["--out", str(tmp / "other.md")])[0], 0)
            self.assertTrue((tmp / "other.md").exists())
            self.assertEqual(run_cli(args + ["--out", str(src)])[0], 2)
            self.assertEqual(src.read_text(encoding="utf-8"), "残す文。消す文。\n")
        finally:
            shutil.rmtree(tmp)

    def test_output_path_skips_taken_names(self):
        tmp = tmpdir()
        try:
            src = tmp / "a.md"
            jp.write_text(src, "x\n")
            self.assertEqual(jp.output_path(src).name, "a.japango.md")
            jp.write_text(tmp / "a.japango.md", "")
            jp.write_text(tmp / "a.japango-2.md", "")
            self.assertEqual(jp.output_path(src).name, "a.japango-3.md")
        finally:
            shutil.rmtree(tmp)

    def test_keep_logged_as_kept(self):
        body, log = self.build("残す文。消す文。\n", [
            {"id": "p1-s1", "route": "keep", "pattern": "型17 残す対比"},
            {"id": "p1-s2", "route": "delete"},
        ])
        self.assertEqual(body, "残す文。\n")
        kept = [c for c in log["changes"] if c["result"] == "残した"]
        self.assertEqual([(c["id"], c["route"]) for c in kept], [("p1-s1", "keep")])
        self.assertEqual(kept[0]["before"], kept[0]["after"])
        self.assertEqual(log["unchanged_ids"], 1)

    def test_two_questions_share_one_mark(self):
        body, log = self.build("一つ目。二つ目。三つ目。\n", [
            {"id": "p1-s1", "route": "ask", "fix": "〔誰が決めたか〕"},
            {"id": "p1-s2", "route": "ask", "fix": "〔誰が判断したか〕〔根拠は何か〕"},
            {"id": "p1-s3", "route": "ask", "fix": "〔根拠は何か〕〔出典は何か〕"},
        ])
        self.assertIn("一つ目。〔確認1〕二つ目。〔確認2〕三つ目。〔確認3〕", body)
        self.assertIn("- 〔確認1〕誰が決めたか\n"
                      "- 〔確認2〕\n  - 誰が判断したか\n  - 根拠は何か\n"
                      "- 〔確認3〕出典は何か\n", body)
        self.assertEqual(log["confirm"][1]["questions"], ["誰が判断したか", "根拠は何か"])


class Marks(unittest.TestCase):
    """The 〔確認n〕 mark sits right after the sentence's end mark, and whatever
    separated it from the next sentence stays as it was."""

    def build(self, text, findings):
        return jp.assemble(jp.units(text), findings)[0].split("\n---\n")[0]

    def test_mark_right_after_period_space_kept(self):
        ask = [{"id": "p1-s1", "route": "ask", "fix": "〔誰か〕"}]
        self.assertEqual(self.build("一つ目。 二つ目。\n", ask), "一つ目。〔確認1〕 二つ目。\n")
        self.assertEqual(self.build("一つ目。二つ目。\n", ask), "一つ目。〔確認1〕二つ目。\n")
        self.assertEqual(self.build("「引用です。」次の文。\n", ask), "「引用です。」〔確認1〕次の文。\n")

    def test_mark_before_hard_break_spaces(self):
        text = "一行目の文。  \n二行目の文。\n三行目。\n"
        self.assertEqual(self.build(text, [{"id": "p1-s1", "route": "ask", "fix": "〔誰か〕"}]),
                         "一行目の文。〔確認1〕  \n二行目の文。\n三行目。\n")
        # a deleted line's question goes to the line before, still right after its period
        self.assertEqual(self.build(text, [{"id": "p1-s2", "route": "delete", "ask": "〔誰か〕"}]),
                         "一行目の文。〔確認1〕  \n三行目。\n")


class Reports(unittest.TestCase):
    def build(self, text, findings):
        return jp.assemble(jp.units(text), findings)

    def test_dangling_reference_reported_not_fixed(self):
        text = "経理課と営業課が集計した。両課は翌日に報告した。結果は全員に共有した。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "delete"}])
        self.assertTrue(body.startswith("両課は翌日に報告した。結果は全員に共有した。\n"))
        self.assertEqual(log["dangling"], [{"deleted": "p1-s1", "words": ["経理課", "営業課"],
                                            "at": "p1-s2", "ref": "両課"}])
        self.assertTrue(body.rstrip("\n").endswith(
            "- 削った文が指していた語が残っていません: 経理課・営業課（p1-s2 の「両課」が受けている。p1-s1 を削った）"))

    def test_dangling_not_reported_when_word_remains_or_nothing_points(self):
        text = "新方式を試した。新方式は速かった。この方式を続ける。\n"
        _, log = self.build(text, [{"id": "p1-s1", "route": "delete"}])
        self.assertEqual(log["dangling"], [])
        text = "経理課が集計した。結果は翌日に出た。担当は全員に共有した。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "delete"}])
        self.assertEqual(log["dangling"], [])
        self.assertNotIn("確認欄", body)
        # a longer head must be part of the word: この設定 does not point at 特定
        text = "原因を特定した。この設定を戻した。\n"
        _, log = self.build(text, [{"id": "p1-s1", "route": "delete"}])
        self.assertEqual(log["dangling"], [])

    def test_orphan_paragraph_reported(self):
        text = "一つ目の文。二つ目の文。\n\n三つ目。四つ目。五つ目。\n\n- 項目一。\n- 項目二。\n"
        body, log = self.build(text, [{"id": "p1-s2", "route": "delete"}, {"id": "p2-s1", "route": "delete"},
                                      {"id": "p3-s2", "route": "delete"}])
        self.assertEqual([(o["para"], o["id"], o["deleted"]) for o in log["orphans"]], [(1, "p1-s1", ["p1-s2"])])
        self.assertIn("- 1 文だけ残った段落: 〔一つ目の文。〕（p1-s1）\n", body)
        self.assertTrue(body.startswith("一つ目の文。\n\n四つ目。五つ目。\n\n- 項目一。\n"))
        # a text left with one paragraph has nothing to float among
        _, log = self.build("一つ目の文。二つ目の文。\n", [{"id": "p1-s2", "route": "delete"}])
        self.assertEqual(log["orphans"], [])

    def test_notes_follow_questions(self):
        text = "経理課と営業課が集計した。両課は翌日に報告した。\n\n次の段落。\n"
        body, _ = self.build(text, [{"id": "p1-s1", "route": "delete", "ask": "〔誰が決めたか〕"}])
        lines = body.split("## 確認欄\n\n")[1].splitlines()
        self.assertEqual(lines[0], "- 〔確認1〕誰が決めたか")
        self.assertTrue(lines[1].startswith("- 削った文が指していた語が残っていません: 経理課・営業課"))
        self.assertEqual(lines[2], "- 1 文だけ残った段落: 〔両課は翌日に報告した。〕（p1-s2）")


if __name__ == "__main__":
    unittest.main()
