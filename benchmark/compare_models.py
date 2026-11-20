"""Compare two EN->MN models on the same test sets with a paired bootstrap
significance test (sacrebleu), so a small BLEU gap can be judged as real or noise.

    python benchmark/compare_models.py nllb_final nllb_v2_laws

Test sets (english, mongolian columns):
  data/test.csv            professional human-rights pairs (held out from both models)
  data/test_vision2050.csv Vision 2050 policy text (seen by neither model)
  data/test_laws.csv       6 laws (NOTE: nllb_v2_laws saw these during training;
                           they are held out only from the later nllb_mn_en run)
Writes benchmark/compare_<a>_vs_<b>.json.
"""
import json
import os
import sys

import pandas as pd
import torch
from sacrebleu.metrics import BLEU, CHRF
from sacrebleu.compat import corpus_bleu  # noqa: F401
from transformers import AutoModelForSeq2SeqLM
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

SETS = ["data/test.csv", "data/test_vision2050.csv", "data/test_laws.csv", "benchmark/new_test/C_unseen_reports.csv"]
import glob as _glob
SETS += sorted(_glob.glob("benchmark/custom_tests/*.csv"))  # 2026-11-15: админы оруулсан шалгалтын материал
try:  # админ самбараас унтраасан шалгалтууд
    import json as _json
    _off = set(_json.load(open("benchmark/tests_disabled.json")))
    SETS = [p for p in SETS if os.path.basename(p) not in _off]
except (OSError, ValueError):
    pass
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
REVERSE = os.environ.get("REVERSE") == "1"  # 2026-11-10: REVERSE=1 -> Монгол -> Англи загваруудыг харьцуулна
SRC, TGT = ("khk_Cyrl", "eng_Latn") if REVERSE else ("eng_Latn", "khk_Cyrl")


def translate(model_dir, sources, bs=32):
    tok = NllbTokenizer.from_pretrained(model_dir, src_lang=SRC, tgt_lang=TGT)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_dir).half().to(DEVICE).eval()
    bos = tok.convert_tokens_to_ids(TGT)
    out = []
    for i in range(0, len(sources), bs):
        enc = tok(sources[i:i + bs], return_tensors="pt", padding=True, truncation=True, max_length=128).to(DEVICE)
        with torch.inference_mode():
            gen = model.generate(**enc, forced_bos_token_id=bos, max_length=128, num_beams=4)
        out += tok.batch_decode(gen, skip_special_tokens=True)
    del model
    torch.cuda.empty_cache()
    return out


def main(a, b):
    results = {}
    for path in SETS:
        df = pd.read_csv(path, keep_default_na=False)
        src, ref = df.english.tolist(), df.mongolian.tolist()
        if REVERSE:
            src, ref = ref, src
        cache = f"benchmark/.hyps_{a}_{b}_{'rev_' if REVERSE else ''}{os.path.basename(path)}.json".replace("/", "_").replace("benchmark_.hyps", "benchmark/.hyps")
        if os.path.exists(cache):
            hyp_a, hyp_b = json.load(open(cache))
        else:
            hyp_a = translate(f"benchmark/{a}/final", src)
            hyp_b = translate(f"benchmark/{b}/final", src)
            json.dump([hyp_a, hyp_b], open(cache, "w"), ensure_ascii=False)
        from sacrebleu.significance import PairedTest
        pt = PairedTest([(a, hyp_a), (b, hyp_b)], {"BLEU": BLEU(), "chrF": CHRF()}, references=[ref],
                        test_type="bs", n_samples=1000)
        _, scores = pt()
        name = os.path.basename(path)
        results[name] = {"n": len(src)}
        print(f"\n== {name} ({len(src)} sentences)")
        for k, sysname in enumerate(scores["System"]):
            row = {}
            for m in [k2 for k2 in scores if k2 != "System"]:
                r = scores[m][k]
                row[m] = {"score": round(r.score, 2), "p_value": r.p_value}
            results[name][sysname] = row
            print(f"  {sysname:<14} " + "  ".join(
                f"{m} {row[m]['score']:6.2f}" + (f" (p={row[m]['p_value']:.3f})" if row[m]['p_value'] is not None else "")
                for m in row))
    with open(os.environ.get("COMPARE_OUT") or f"benchmark/compare_{a}_vs_{b}.json".replace("/", "_").replace("benchmark_compare", "benchmark/compare"), "w") as f:
        json.dump(results, f, indent=2, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
