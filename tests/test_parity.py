"""japango.mjs (the Node.js port) must write exactly what japango.py writes.

Each folder under tests/parity/ holds a source.md and the finder runs recorded
in earlier tests (findings1.jsonl, findings2.jsonl). Both scripts run ids,
merge (agree, union, and one run alone) and assemble on it, and every file and
printed summary is compared byte for byte. Skipped when node is not installed.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_japango import ROOT, tmpdir

SCRIPTS = ROOT / "skills" / "japango" / "scripts"
CASES = ROOT / "tests" / "parity"
NODE = shutil.which("node")


def run(lang, args, work=None):
    """(exit code, what the command printed), with the work folder shown as <work>."""
    cmd = [sys.executable, str(SCRIPTS / "japango.py")] if lang == "py" else [NODE, str(SCRIPTS / "japango.mjs")]
    p = subprocess.run(cmd + [str(a) for a in args], capture_output=True)
    printed = p.stdout.decode("utf-8").replace("\r\n", "\n")
    if work is not None:
        printed = printed.replace(json.dumps(str(work), ensure_ascii=False)[1:-1], "<work>")
    return p.returncode, printed


@unittest.skipUnless(NODE, "node is not installed")
class Parity(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def same(self, a, b, what):
        self.assertEqual(Path(a).read_bytes(), Path(b).read_bytes(), what)

    def check_case(self, case):
        base = self.tmp / case.name
        base.mkdir()
        src = base / "source.md"
        shutil.copy(case / "source.md", src)
        runs = sorted(case.glob("findings*.jsonl"))
        work = {lang: base / lang for lang in ("py", "js")}
        out = {}
        for lang, w in work.items():
            out[lang] = [run(lang, ["ids", src, w], w)]
        self.assertEqual(out["py"], out["js"], f"{case.name}: ids output")
        for name in ("source.units.jsonl", "source.annotated.txt", "source.orig.md"):
            self.same(work["py"] / name, work["js"] / name, f"{case.name}: {name}")

        plans = [("agree", runs), ("union", runs), ("agree", runs[:1])]
        for k, (mode, fs) in enumerate(plans):
            for lang, w in work.items():
                merged, log, md = w / f"m{k}.jsonl", w / f"log{k}.json", w / f"out{k}.md"
                out[lang].append(run(lang, ["merge", w / "source.units.jsonl", merged, mode, *fs], w))
                out[lang].append(run(lang, ["assemble", w / "source.units.jsonl", merged, log, "--out", md], w))
            self.assertEqual(out["py"], out["js"], f"{case.name}: {mode} {len(fs)} run(s), printed")
            for name in (f"m{k}.jsonl", f"log{k}.json", f"out{k}.md"):
                self.same(work["py"] / name, work["js"] / name, f"{case.name}: {mode} {len(fs)} run(s), {name}")

        # without --out both write next to the source: .japango.md, then .japango-2.md
        printed = [json.loads(run(lang, ["assemble", work[lang] / "source.units.jsonl",
                                         work[lang] / "m0.jsonl", work[lang] / "logd.json"])[1])["out"]
                   for lang in ("py", "js")]
        self.assertEqual([Path(p).name for p in printed], ["source.japango.md", "source.japango-2.md"])
        self.same(printed[0], printed[1], f"{case.name}: default output")

    def test_cases(self):
        cases = sorted(p for p in CASES.iterdir() if (p / "source.md").exists())
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case=case.name):
                self.check_case(case)

    def test_refuses_to_overwrite_source(self):
        src = self.tmp / "source.md"
        shutil.copy(CASES / "edge" / "source.md", src)
        for lang in ("py", "js"):
            w = self.tmp / lang
            run(lang, ["ids", src, w])
            (w / "m.jsonl").write_text("", encoding="utf-8")
            code, _ = run(lang, ["assemble", w / "source.units.jsonl", w / "m.jsonl", w / "l.json", "--out", src])
            self.assertEqual(code, 2, lang)
        self.same(src, CASES / "edge" / "source.md", "source untouched")


if __name__ == "__main__":
    unittest.main()
