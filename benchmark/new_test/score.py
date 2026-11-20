"""Score translated new-test docx files.
    python benchmark/new_test/score.py A_general_FLORES  <translated.docx> [<translated2.docx> ...]
Each docx = a translation of benchmark/new_test/<set>_EN.docx (Google Translate, our website, ...).
Sentences are matched by their "N." number; if numbers were lost, by order.
"""
import json, re, sys
from sacrebleu.metrics import BLEU, CHRF
sys.path.insert(0, "webapp")
import documents  # noqa: E402

name, files = sys.argv[1], sys.argv[2:]
ref = [m for _, m in json.load(open(f"benchmark/new_test/{name}_ref.json"))]
for f in files:
    paras = [p for p in documents.read_document(f, ".docx")[0] if p.strip()]
    num = {int(m.group(1)): m.group(2).strip() for p in paras for m in [re.match(r"\s*(\d+)\s*[.)]\s*(.*)", p)] if m}
    hyp = [num.get(i + 1, "") for i in range(len(ref))] if len(num) >= 0.9 * len(ref) else (paras + [""] * len(ref))[:len(ref)]
    print(f"{f}: {sum(bool(h) for h in hyp)}/{len(ref)} matched  BLEU {BLEU().corpus_score(hyp, [ref]).score:.2f}  "
          f"chrF {CHRF().corpus_score(hyp, [ref]).score:.2f}")
