"""Score a model on FLORES-200 devtest (1,012 general-domain Wikipedia-style sentences) --
a standard benchmark that none of our models trained on, to measure GENERAL translation
quality (not just human-rights text).

    python benchmark/eval_flores.py nllb_v2_laws en-mn
    python benchmark/eval_flores.py nllb_mn_en   mn-en
    python benchmark/eval_flores.py base         en-mn   # untuned facebook/nllb-200-distilled-600M
"""
import json
import os
import sys

import torch
from sacrebleu.metrics import BLEU, CHRF
from transformers import AutoModelForSeq2SeqLM
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

FLORES = "data/raw_general/flores200_dataset/devtest"
CODES = {"en": "eng_Latn", "mn": "khk_Cyrl"}


def main(name, direction):
    src, tgt = direction.split("-")
    s = open(f"{FLORES}/{CODES[src]}.devtest", encoding="utf-8").read().splitlines()
    r = open(f"{FLORES}/{CODES[tgt]}.devtest", encoding="utf-8").read().splitlines()
    path = "facebook/nllb-200-distilled-600M" if name == "base" else f"benchmark/{name}/final"
    tok = NllbTokenizer.from_pretrained(path, src_lang=CODES[src], tgt_lang=CODES[tgt])
    model = AutoModelForSeq2SeqLM.from_pretrained(path).half().to("cuda").eval()
    bos = tok.convert_tokens_to_ids(CODES[tgt])
    hyp = []
    for i in range(0, len(s), 32):
        enc = tok(s[i:i + 32], return_tensors="pt", padding=True, truncation=True, max_length=256).to("cuda")
        with torch.inference_mode():
            out = model.generate(**enc, forced_bos_token_id=bos, max_length=256, num_beams=4)
        hyp += tok.batch_decode(out, skip_special_tokens=True)
    bleu, chrf = BLEU().corpus_score(hyp, [r]).score, CHRF().corpus_score(hyp, [r]).score
    os.makedirs("benchmark/flores", exist_ok=True)
    json.dump(hyp, open(f"benchmark/flores/{name}_{direction}.json", "w"), ensure_ascii=False)
    print(f"{name:<14} {direction}  FLORES devtest  BLEU {bleu:.2f}  chrF {chrf:.2f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
