import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "japango" / "scripts"))

import japango as jp  # noqa: E402

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
        tmp = Path(tempfile.mkdtemp(dir=ROOT / "tests"))
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

    def test_cli_assemble_writes_log(self):
        tmp = Path(tempfile.mkdtemp(dir=ROOT / "tests"))
        try:
            src = tmp / "d.md"
            jp.write_text(src, "残す文。消す文。\n")
            jp.main(["ids", str(src), str(tmp)])
            jp.write_text(tmp / "f.jsonl", json.dumps({"id": "p1-s2", "route": "delete"}) + "\n")
            jp.main(["merge", str(tmp / "d.units.jsonl"), str(tmp / "m.jsonl"), "agree", str(tmp / "f.jsonl")])
            jp.main(["assemble", str(tmp / "d.units.jsonl"), str(tmp / "m.jsonl"),
                     str(tmp / "out.md"), str(tmp / "log.json")])
            self.assertEqual((tmp / "out.md").read_text(encoding="utf-8"), "残す文。\n")
            log = json.loads((tmp / "log.json").read_text(encoding="utf-8"))
            self.assertEqual(log["changes"][0]["before"], "消す文。")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
