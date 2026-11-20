"""Evaluate the m2m100 and nllb benchmark models on the held-out test set:
BLEU, chrF, per-sentence latency, and model size on disk.
"""
import json
import os
import sys
import time

import evaluate
import pandas as pd
import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_bench import CONFIGS, forced_bos_id

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def dir_size_mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total / (1024 * 1024)


def evaluate_model(key):
    cfg = CONFIGS[key]
    model_dir = f"benchmark/{key}/final"

    if cfg["model_name"].startswith("facebook/nllb"):
        from transformers.models.nllb.tokenization_nllb import NllbTokenizer
        tokenizer = NllbTokenizer.from_pretrained(
            model_dir, src_lang=cfg["src_lang"], tgt_lang=cfg["tgt_lang"]
        )
    else:
        tokenizer = AutoTokenizer.from_pretrained(
            model_dir, src_lang=cfg["src_lang"], tgt_lang=cfg["tgt_lang"]
        )

    model = AutoModelForSeq2SeqLM.from_pretrained(model_dir).to(DEVICE)
    model.eval()
    model.generation_config.forced_bos_token_id = forced_bos_id(tokenizer, cfg)

    test_df = pd.read_csv("data/test.csv")
    src_col, ref_col = ("mongolian", "english") if cfg.get("reverse") else ("english", "mongolian")
    sources = test_df[src_col].tolist()
    references = test_df[ref_col].tolist()

    predictions = []
    start = time.time()
    batch_size = 16
    with torch.no_grad():
        for i in range(0, len(sources), batch_size):
            batch = sources[i : i + batch_size]
            enc = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=128).to(DEVICE)
            out = model.generate(**enc, max_length=128, num_beams=4)
            predictions.extend(tokenizer.batch_decode(out, skip_special_tokens=True))
    elapsed = time.time() - start
    ms_per_sentence = (elapsed / len(sources)) * 1000

    bleu = evaluate.load("sacrebleu")
    chrf = evaluate.load("chrf")
    bleu_score = bleu.compute(predictions=predictions, references=[[r] for r in references])
    chrf_score = chrf.compute(predictions=predictions, references=[[r] for r in references])

    size_mb = dir_size_mb(model_dir)

    result = {
        "model": cfg["model_name"],
        "test_bleu": bleu_score["score"],
        "test_chrf": chrf_score["score"],
        "ms_per_sentence": ms_per_sentence,
        "model_size_mb": size_mb,
        "n_test": len(sources),
    }

    with open(f"benchmark/{key}/test_results.json", "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    # save a handful of example translations for spot-checking
    examples = [
        {"english": s, "reference": r, "prediction": p}
        for s, r, p in list(zip(sources, references, predictions))[:15]
    ]
    with open(f"benchmark/{key}/sample_predictions.json", "w") as f:
        json.dump(examples, f, ensure_ascii=False, indent=2)

    return result


if __name__ == "__main__":
    results = {}
    for key in CONFIGS:
        print(f"Evaluating {key}...")
        results[key] = evaluate_model(key)
        print(json.dumps(results[key], indent=2))

    print("\n=== Comparison ===")
    print(f"{'model':<10} {'BLEU':>8} {'chrF':>8} {'ms/sent':>10} {'size(MB)':>10}")
    for key, r in results.items():
        print(f"{key:<10} {r['test_bleu']:>8.2f} {r['test_chrf']:>8.2f} {r['ms_per_sentence']:>10.1f} {r['model_size_mb']:>10.0f}")
