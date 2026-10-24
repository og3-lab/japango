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

    def test_union_single_rewrite_is_listed(self):
        a = [{"id": "p1-s2", "route": "rewrite", "fix": "新しい文。"},
             {"id": "p1-s3", "route": "rewrite", "fix": "別の文。"},
             {"id": "p1-s4", "route": "rewrite", "fix": "両方の文。"}]
        b = [{"id": "p1-s4", "route": "rewrite", "fix": "両方の文。"}]
        merged, _ = jp.merge(self.us, [a, b], "union")
        flags = {r["id"]: r.get("one_run", False) for r in merged}
        self.assertEqual(flags, {"p1-s2": True, "p1-s3": True, "p1-s4": False})
        body, log = jp.assemble([dict(u) for u in self.us], merged)
        self.assertNotIn("〔", body)
        self.assertIn("- 片方の回だけが直した文: 原文に無い行動・出来事が増えていないか（p1-s2「新しい文。」）\n", body)
        self.assertIn("（p1-s3「別の文。」）\n", body)
        self.assertNotIn("p1-s4「", body)
        self.assertEqual([x["id"] for x in log["one_run"]], ["p1-s2", "p1-s3"])
        merged, _ = jp.merge(self.us, [a, b], "agree")
        self.assertTrue(all(not r.get("one_run") for r in merged))

    def test_single_rewrite_of_heading_is_listed(self):
        us = [{"id": "p1-s1", "text": "前の節の文。", "para": 1},
              {"id": "p2-s1", "text": "## 見出し", "para": 2},
              {"id": "p3-s1", "text": "見出しの下の文。", "para": 3}]
        a = [{"id": "p2-s1", "route": "rewrite", "fix": "## 新しい見出し"}]
        merged, _ = jp.merge(us, [a, []], "union")
        self.assertTrue(merged[0]["one_run"])
        body, _ = jp.assemble([dict(u) for u in us], merged)
        self.assertIn("（p2-s1「## 新しい見出し」）", body)
        self.assertNotIn("〔", body)

    def test_align_conflict_takes_first_run_and_says_so(self):
        # both runs rewrite, but toward different words: not an agreement on the word
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21e 同じ動作を別の動詞で呼ぶ",
              "align": "質問する", "fix": "あなたに質問します。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21e", "align": "聞く",
              "fix": "あなたに聞きます。"}]
        reading = "そろえる語が回で分かれた（「質問する」・「聞く」）。先の回の言い方で直した"
        for mode in ("agree", "union"):
            merged, warnings = jp.merge(self.us, [a, b], mode)
            row = next(r for r in merged if r["id"] == "p1-s2")
            self.assertEqual((row["route"], row["fix"]), ("rewrite", "あなたに質問します。"), mode)
            self.assertEqual(row["reading"], reading, mode)
            self.assertIn("align conflict p1-s2: 質問する / 聞く -> first run", warnings)
        body, _ = jp.assemble([dict(u) for u in self.us], merged)
        self.assertIn("文1。あなたに質問します。文3。", body)
        self.assertIn(f"- 読みを選んだ: {reading}（p1-s2「あなたに質問します。」）\n", body)

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
        self.assertEqual((row["route"], row["fix"]), ("rewrite", "あなたに質問する、を決めます。"))
        self.assertEqual(row["reading"], "そろえる語が回で分かれた（「質問する」とほかの回の言い方）。先の回の言い方で直した")

    def test_align_only_for_types_21(self):
        # other types and 21b/21c never compare align; a missing align is reported
        a = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21c", "align": "x", "fix": "一。"},
             {"id": "p1-s3", "route": "rewrite", "pattern": "型21e", "fix": "二。"}]
        b = [{"id": "p1-s2", "route": "rewrite", "pattern": "型21c", "align": "y", "fix": "一。"},
             {"id": "p1-s3", "route": "rewrite", "pattern": "型21e", "fix": "二。"}]
        merged, warnings = jp.merge(self.us, [a, b], "agree")
        self.assertEqual([r["route"] for r in merged], ["rewrite", "rewrite"])
        self.assertEqual(len([w for w in warnings if "has no align" in w]), 2)

    def test_keep_wins(self):
        a = [{"id": "p1-s1", "route": "delete"}, {"id": "p1-s3", "route": "rewrite", "fix": "三。"}]
        b = [{"id": "p1-s1", "route": "keep"}, {"id": "p1-s3", "route": "rewrite", "fix": "参。"}]
        for mode in ("agree", "union"):
            merged, _ = jp.merge(self.us, [a, b], mode)
            routes = {r["id"]: r["route"] for r in merged}
            self.assertEqual(routes, {"p1-s1": "keep", "p1-s3": "rewrite"}, mode)

    def test_ask_is_not_a_route_and_unknown_id_warned(self):
        a = [{"id": "p1-s1", "route": "ask", "fix": "〔誰が決めたか〕"},
             {"id": "p9-s9", "route": "delete"}]
        merged, warnings = jp.merge(self.us, [a], "agree")
        self.assertEqual(merged, [])
        self.assertEqual(warnings, ["run 1: skipped 'p1-s1' (route 'ask')",
                                    "run 1: skipped 'p9-s9' (route 'delete')"])
        # a merged file from an earlier version: assemble skips its ask rows
        body, log = jp.assemble([dict(u) for u in self.us], [{"id": "p1-s1", "route": "ask", "fix": "〔誰か〕"}])
        self.assertEqual(log["skipped"], ["p1-s1"])
        self.assertEqual(log["changes"], [])
        self.assertNotIn("〔", body)

    def test_keep_twice_stays_as_keep_row(self):
        a = [{"id": "p1-s1", "route": "keep", "pattern": "型17"}, {"id": "p1-s2", "route": "delete"}]
        b = [{"id": "p1-s1", "route": "keep"}, {"id": "p1-s2", "route": "keep"}]
        for mode in ("agree", "union"):
            merged, _ = jp.merge(self.us, [a, b], mode)
            self.assertEqual([(r["id"], r["route"]) for r in merged],
                             [("p1-s1", "keep"), ("p1-s2", "keep")], mode)

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

    def test_reading_goes_with_the_rewrite_taken(self):
        a = [{"id": "p1-s2", "route": "rewrite", "fix": "二の文。", "reading": "「それ」を\n直前の一の文と読んだ "},
             {"id": "p1-s3", "route": "delete", "reading": "削る文には付かない"}]
        b = [{"id": "p1-s2", "route": "rewrite", "fix": "弐の文。", "reading": "別の読み"},
             {"id": "p1-s3", "route": "delete"}]
        merged, _ = jp.merge(self.us, [a, b], "agree")
        self.assertEqual(merged, [
            {"id": "p1-s2", "route": "rewrite", "fix": "二の文。", "pattern": "", "reading": "「それ」を 直前の一の文と読んだ"},
            {"id": "p1-s3", "route": "delete", "fix": "", "pattern": ""}])
        # one run's reading alone is not an agreement
        merged, _ = jp.merge(self.us, [a, []], "agree")
        self.assertEqual(merged, [])


class Assemble(unittest.TestCase):
    def build(self, text, findings):
        us = jp.units(text)
        return jp.assemble(us, findings)

    def test_readings_follow_body_order(self):
        text = "一つ目。二つ目。三つ目。\n\n四つ目。\n"
        body, log = self.build(text, [
            {"id": "p2-s1", "route": "rewrite", "fix": "四番目。", "reading": "四の読み"},
            {"id": "p1-s1", "route": "rewrite", "fix": "一番目。", "reading": "一の読み", "one_run": True},
            {"id": "p1-s3", "route": "delete", "reading": "付かない"},
        ])
        self.assertEqual(body, "一番目。二つ目。\n\n四番目。\n\n---\n\n## 読みを選んだ箇所・見直す箇所\n\n"
                               "- 読みを選んだ: 一の読み（p1-s1「一番目。」）\n"
                               "- 読みを選んだ: 四の読み（p2-s1「四番目。」）\n"
                               "- 片方の回だけが直した文: 原文に無い行動・出来事が増えていないか（p1-s1「一番目。」）\n")
        self.assertEqual([x["id"] for x in log["readings"]], ["p1-s1", "p2-s1"])
        self.assertEqual(log["readings"][0], {"id": "p1-s1", "reading": "一の読み", "after": "一番目。"})

    def test_reverted_rewrite_lists_nothing(self):
        text = "待ち時間は 1 時間 25 分ほどだった。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "rewrite", "fix": "待ち時間は 85 分だった。",
                                       "reading": "読み", "one_run": True}])
        self.assertEqual(body, text)
        self.assertEqual((log["readings"], log["one_run"]), ([], []))

    def test_new_number_reverts(self):
        text = "待ち時間は 1 時間 25 分ほどだった。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "rewrite", "fix": "待ち時間は 85 分だった。"}])
        self.assertEqual(body, text)
        self.assertEqual(log["reverted"][0]["words"], ["85"])

    def test_mama_desu_is_not_a_new_time_word(self):
        text = "会議は、それぞれの書式で整えます。\n"
        body, log = self.build(text, [{"id": "p1-s1", "route": "rewrite", "fix": "会議は、それぞれの書式のままです。"}])
        self.assertEqual(log["reverted"], [])
        self.assertIn("書式のままです。", body)
        body, log = self.build(text, [{"id": "p1-s1", "route": "rewrite", "fix": "会議は、月末まで書式で整えます。"}])
        self.assertEqual(log["reverted"][0]["words"], ["まで"])

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
        self.assertNotIn("## 読みを選んだ箇所", body)
        # a longer head must be part of the word: この設定 does not point at 特定
        text = "原因を特定した。この設定を戻した。\n"
        _, log = self.build(text, [{"id": "p1-s1", "route": "delete"}])
        self.assertEqual(log["dangling"], [])

    def test_restatement_deletion_with_new_word_reported(self):
        text = "つけるんは、口座とはつなぎません。口座の残高を読む機能ではありません。読むのはレシートだけです。\n"
        body, log = self.build(text, [{"id": "p1-s2", "route": "delete", "pattern": "型6 言い直しだけの文"}])
        self.assertEqual(log["lost"], [{"deleted": "p1-s2", "words": ["残高", "機能"],
                                        "text": "口座の残高を読む機能ではありません。"}])
        self.assertTrue(body.rstrip("\n").endswith(
            "- 言い直しとして削った文にしか無い語: 残高・機能（p1-s2「口座の残高を読む機能ではありません。」）"))
        # the sentence stays deleted: reported, not put back
        self.assertTrue(body.startswith("つけるんは、口座とはつなぎません。読むのはレシートだけです。\n"))

    def test_restatement_check_only_for_type6_and_new_words(self):
        text = "経理課が集計した。経理課が集計を終えた。結果は翌日に出た。\n"
        # every word of the deleted sentence is still in the body
        _, log = self.build(text, [{"id": "p1-s2", "route": "delete", "pattern": "型6 言い直しだけの文"}])
        self.assertEqual(log["lost"], [])
        # a closing line (型 6a is another type) is not a restatement
        text = "経理課が集計した。きっと大きな成果になるはずだ。\n"
        for pattern in ("型10 締めの一文", "型6a 接続語", ""):
            _, log = self.build(text, [{"id": "p1-s2", "route": "delete", "pattern": pattern}])
            self.assertEqual(log["lost"], [])
        # words already reported as dangling are not listed twice
        text = "経理課と営業課が集計した。両課は翌日に集計を報告した。\n"
        _, log = self.build(text, [{"id": "p1-s1", "route": "delete", "pattern": "型6"}])
        self.assertEqual(len(log["dangling"]), 1)
        self.assertEqual(log["lost"], [])

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

    def test_notes_follow_readings(self):
        text = "経理課と営業課が集計した。両課は翌日に報告した。\n\n次の段落。\n"
        body, _ = self.build(text, [{"id": "p1-s1", "route": "delete"},
                                    {"id": "p2-s1", "route": "rewrite", "fix": "次の段落です。", "reading": "読み"}])
        lines = body.split("## 読みを選んだ箇所・見直す箇所\n\n")[1].splitlines()
        self.assertEqual(lines[0], "- 読みを選んだ: 読み（p2-s1「次の段落です。」）")
        self.assertTrue(lines[1].startswith("- 削った文が指していた語が残っていません: 経理課・営業課"))
        self.assertEqual(lines[2], "- 1 文だけ残った段落: 〔両課は翌日に報告した。〕（p1-s2）")


class Join(unittest.TestCase):
    TEXT = ("今は、ほとんどがボットから入ります。電話で頼む人も、まだ週に数人います。\n\n"
            "窓口は朝九時に開きます。担当は二人です。昼は一人になります。\n\n"
            "- 項目の一つ目です。\n- 項目の二つ目です。\n")
    FIX = "今は、ほとんどがボットから入りますが、電話で頼む人もまだ週に数人います。"

    def join(self, i, span, fix):
        return {"id": i, "route": "rewrite", "fix": fix, "join": span}

    def run_merge(self, runs, mode="agree"):
        us = jp.units(self.TEXT)
        merged, warnings = jp.merge(us, runs, mode)
        body, log = jp.assemble(jp.units(self.TEXT), merged)
        return merged, warnings, body, log

    def test_join_replaces_first_and_empties_rest(self):
        j = self.join("p1-s1", ["p1-s1", "p1-s2"], self.FIX)
        merged, warnings, body, log = self.run_merge([[j], [j]])
        self.assertEqual(warnings, [])
        self.assertEqual(merged[0]["join"], ["p1-s1", "p1-s2"])
        self.assertTrue(body.startswith(self.FIX + "\n\n窓口は"))
        self.assertEqual(log["changes"][0]["join"], ["p1-s1", "p1-s2"])
        self.assertEqual(log["changes"][0]["before"], "今は、ほとんどがボットから入ります。電話で頼む人も、まだ週に数人います。")
        # joining is not deleting: no lone-sentence paragraph, no dangling word
        self.assertEqual(log["orphans"], [])
        self.assertEqual(log["unchanged_ids"], len(jp.units(self.TEXT)) - 2)

    def test_join_must_be_contiguous_in_one_prose_paragraph(self):
        runs = [[self.join("p2-s1", ["p2-s1", "p2-s3"], "窓口は朝九時に開き、昼は一人になります。"),
                 self.join("p1-s2", ["p1-s2", "p2-s1"], "電話で頼む人もいて、窓口は朝九時に開きます。"),
                 self.join("p3-s1", ["p3-s1", "p3-s2"], "- 項目の一つ目と二つ目です。")]]
        merged, warnings, body, _ = self.run_merge(runs, "union")
        self.assertEqual(merged, [])
        self.assertEqual(warnings, ["run 1: join not contiguous p2-s1", "run 1: join not contiguous p1-s2",
                                    "run 1: join over lines p3-s1"])
        self.assertEqual(body, self.TEXT)

    def test_agree_needs_the_other_run_to_edit_the_span(self):
        j = self.join("p1-s1", ["p1-s1", "p1-s2"], self.FIX)
        merged, _, _, _ = self.run_merge([[j], []])
        self.assertEqual(merged, [])
        other = {"id": "p1-s2", "route": "delete"}
        merged, _, body, _ = self.run_merge([[j], [other]])
        self.assertEqual([(m["id"], m.get("join")) for m in merged], [("p1-s1", ["p1-s1", "p1-s2"])])
        self.assertTrue(body.startswith(self.FIX + "\n"))
        # union takes it, and lists it since one run proposed it
        merged, _, _, _ = self.run_merge([[j], []], "union")
        self.assertTrue(merged[0]["one_run"])

    def test_keep_in_span_drops_join(self):
        j = self.join("p1-s1", ["p1-s1", "p1-s2"], self.FIX)
        keep = {"id": "p1-s2", "route": "keep"}
        for mode in ("agree", "union"):
            merged, warnings, body, _ = self.run_merge([[j], [keep]], mode)
            self.assertEqual([m["route"] for m in merged], ["keep"], mode)
            self.assertEqual(warnings, ["run 1: join kept p1-s1"], mode)
            self.assertEqual(body, self.TEXT, mode)

    def test_span_differs_takes_first_run(self):
        a = self.join("p2-s2", ["p2-s2", "p2-s3"], "担当は二人で、昼は一人になります。")
        b = self.join("p2-s1", ["p2-s1", "p2-s2"], "窓口は朝九時に開き、担当は二人です。")
        merged, warnings, body, _ = self.run_merge([[a], [b]])
        self.assertEqual([m["id"] for m in merged], ["p2-s2"])
        self.assertEqual(warnings, ["run 2: join span differs p2-s1"])
        self.assertIn("窓口は朝九時に開きます。担当は二人で、昼は一人になります。\n", body)

    def test_union_join_drops_edits_inside(self):
        j = self.join("p1-s1", ["p1-s1", "p1-s2"], self.FIX)
        rs = [{"id": "p1-s2", "route": "rewrite", "fix": "電話の人もいます。"}]
        merged, warnings, body, _ = self.run_merge([[j], rs], "union")
        self.assertEqual(warnings, ["run 2: join p1-s1 drops rewrite p1-s2"])
        self.assertEqual([(m["id"], m["route"]) for m in merged], [("p1-s1", "rewrite")])
        # the other run edited a sentence in the span: not a single run's rewrite
        self.assertNotIn("one_run", merged[0])
        self.assertTrue(body.startswith(self.FIX + "\n\n窓口は"))

    def test_added_fact_puts_back_every_sentence(self):
        j = self.join("p1-s1", ["p1-s1", "p1-s2"], "ボットが増えたため、電話で頼む人は週に3人です。")
        _, _, body, log = self.run_merge([[j], [j]])
        self.assertEqual(body, self.TEXT)
        self.assertEqual(log["reverted"][0]["join"], ["p1-s1", "p1-s2"])
        self.assertEqual(log["reverted"][0]["words"], ["ため", "3"])
        self.assertEqual(log["changes"], [])

    def test_join_carries_its_reading(self):
        j = dict(self.join("p1-s1", ["p1-s1", "p1-s2"], self.FIX), reading="電話の人を今の話と読んだ")
        merged, _, body, log = self.run_merge([[j], [j]])
        self.assertEqual(merged[0]["reading"], "電話の人を今の話と読んだ")
        self.assertEqual(log["readings"], [{"id": "p1-s1", "reading": "電話の人を今の話と読んだ", "after": self.FIX}])
        self.assertIn(f"- 読みを選んだ: 電話の人を今の話と読んだ（p1-s1「{self.FIX}」）\n", body)


class SecondRound(unittest.TestCase):
    """ids, merge and assemble run again on a text assemble wrote (the reader's round)."""
    TEXT = "窓口は朝九時に開きます。担当は二人です。\n\n昼は一人になります。返事は翌日に送ります。急ぎは当日です。\n"
    ROUND1 = [{"id": "p1-s1", "route": "rewrite", "fix": "窓口は朝九時に開けます。", "reading": "窓口のための時刻と読んだ"},
              {"id": "p1-s2", "route": "rewrite", "fix": "担当は二人。", "reading": "二人を担当の 2 人と読んだ"}]
    HEAD = "---\n\n## 読みを選んだ箇所・見直す箇所\n\n"
    LIST = ("- 読みを選んだ: 窓口のための時刻と読んだ（p1-s1「窓口は朝九時に開けます。」）\n"
            "- 読みを選んだ: 二人を担当の 2 人と読んだ（p1-s2「担当は二人。」）\n")

    def first(self):
        us = jp.units(self.TEXT)
        merged, _ = jp.merge(us, [self.ROUND1])
        return jp.assemble(us, merged)[0]

    def second(self, runs, mode="agree"):
        text = self.first()
        merged, warnings = jp.merge(jp.units(text), runs, mode)
        body, log = jp.assemble(jp.units(text), merged)
        return merged, warnings, body, log

    def test_list_is_one_raw_unit(self):
        text = self.first()
        self.assertTrue(text.endswith("急ぎは当日です。\n\n" + self.HEAD + self.LIST))
        us = jp.units(text)
        box = us[-1]
        self.assertTrue(box["raw"] and box["confirm"])
        self.assertEqual(box["text"], self.HEAD + self.LIST.rstrip("\n"))
        self.assertEqual(jp.render(us), jp.expected_body(text))
        # the finder sees neither the list nor its heading
        self.assertNotIn("読みを選んだ", "".join(u["text"] for u in us if not u.get("raw")))
        self.assertEqual([u["text"] for u in us if u["para"] == 1], ["窓口は朝九時に開けます。", "担当は二人。"])

    def test_list_not_in_assemble_form_is_split_as_before(self):
        text = self.first().replace("- 読みを選んだ: 二人を", "二人を")
        self.assertFalse(any(u.get("confirm") for u in jp.units(text)))

    def test_second_readings_go_to_the_end_of_the_first_list(self):
        r = {"id": "p2-s2", "route": "rewrite", "fix": "返事は翌日に送る決まりです。", "reading": "決まりの説明と読んだ"}
        _, _, body, log = self.second([[r]])
        self.assertEqual(body.count("## 読みを選んだ箇所・見直す箇所"), 1)
        self.assertTrue(body.endswith(self.LIST + "- 読みを選んだ: 決まりの説明と読んだ（p2-s2「返事は翌日に送る決まりです。」）\n"))
        self.assertEqual([x["id"] for x in log["readings"]], ["p2-s2"])

    def test_notes_go_to_the_same_list(self):
        _, _, body, log = self.second([[{"id": "p2-s3", "route": "delete"}, {"id": "p2-s2", "route": "delete"}]])
        self.assertEqual(body.count("## 読みを選んだ箇所・見直す箇所"), 1)
        self.assertTrue(body.endswith(self.LIST + "- 1 文だけ残った段落: 〔昼は一人になります。〕（p2-s1）\n"))

    def test_words_of_the_list_are_not_the_original(self):
        # 「ため」 and 「2」 are only in the first round's list
        for fix, words in (("昼は休憩のため一人になります。", ["ため"]), ("昼は2人になります。", ["2"])):
            r = {"id": "p2-s1", "route": "rewrite", "fix": fix}
            _, _, body, log = self.second([[r]])
            self.assertEqual(log["reverted"][0]["words"], words, fix)
            self.assertEqual(body, self.first(), fix)

    def test_join_in_the_second_round(self):
        j = {"id": "p2-s2", "route": "rewrite", "fix": "返事は翌日に送り、急ぎは当日です。", "join": ["p2-s2", "p2-s3"]}
        _, warnings, body, _ = self.second([[j]])
        self.assertEqual(warnings, [])
        self.assertIn("昼は一人になります。返事は翌日に送り、急ぎは当日です。\n\n---\n", body)


class Extract(unittest.TestCase):
    PAGE = """<!doctype html><html><head><title>t</title><style>p{color:red}</style></head><body>
<header><h1>見出し<br>二行目</h1><script>var a = "<p>x</p>";</script></header>
<p>本文の文です。<code>npm i</code> を打ちます。</p>
<div class="btns"><a class="btn b1" href="/a.zip" download>zip をダウンロード</a>
<a href="/x">ふつうのリンク</a></div>
<div class="ask"><code>/plugin install a@b</code><button type="button">コピー</button></div>
<ul><li>一つ目</li><li>二つ<b>目</b></li></ul>
<dl><dt>見出し語</dt><dd>説明&amp;続き&#x3002;</dd></dl>
<img src="a.png" alt="図 1"><svg><text>捨てる</text></svg>
<pre>line 1

line 2</pre>
</body></html>"""

    def test_extract(self):
        md, counts = jp.extract(self.PAGE)
        self.assertEqual(md, "# 見出し二行目\n\n本文の文です。`npm i` を打ちます。\n\n"
                             "〔ボタン: zip をダウンロード → /a.zip〕\n\nふつうのリンク\n\n"
                             "〔コード: /plugin install a@b〕\n〔ボタン: コピー〕\n\n- 一つ目\n- 二つ目\n\n"
                             "見出し語\n\n説明&続き。\n\n〔画像: 図 1〕\n\n〔コード: line 1\nline 2〕\n")
        self.assertEqual((counts["ボタン"], counts["コード"], counts["画像"]), (2, 2, 1))

    def test_marks_are_kept_as_is(self):
        md, _ = jp.extract(self.PAGE)
        us = jp.units(md)
        self.assertEqual(jp.render(us), md)
        marks = [u for u in us if "〔" in u["text"]]
        self.assertTrue(marks and all(u.get("raw") for u in marks))
        body, _ = jp.assemble(jp.units(md), [])
        self.assertEqual(body, md)


if __name__ == "__main__":
    unittest.main()
