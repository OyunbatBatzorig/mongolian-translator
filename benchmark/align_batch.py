"""Run pdf_align on an explicit list of (en_path, mn_path, out_csv) triples,
passed as a JSON file, to avoid shell-quoting issues with filenames that
contain spaces or embedded newlines.

Usage:
    python benchmark/align_batch.py jobs.json
"""
import json
import sys

from pdf_align import extract_sentences, THRESHOLD, MIN_CYRILLIC_FRACTION, cyrillic_fraction
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

jobs = json.load(open(sys.argv[1]))
model = SentenceTransformer("sentence-transformers/LaBSE", device="cpu")

for job in jobs:
    en_path, mn_path, out_path = job["en"], job["mn"], job["out"]
    print(f"=== {out_path} ===")
    en_sents = extract_sentences(en_path)
    mn_sents = extract_sentences(mn_path)
    print(f"Extracted {len(en_sents)} EN sentences, {len(mn_sents)} MN sentences")

    en_emb = model.encode(en_sents, batch_size=64, show_progress_bar=False,
                           convert_to_numpy=True, normalize_embeddings=True)
    mn_emb = model.encode(mn_sents, batch_size=64, show_progress_bar=False,
                           convert_to_numpy=True, normalize_embeddings=True)

    sim = en_emb @ mn_emb.T
    en_best = sim.argmax(axis=1)
    mn_best = sim.argmax(axis=0)

    pairs = []
    for i, j in enumerate(en_best):
        if mn_best[j] == i and sim[i, j] >= THRESHOLD:
            if cyrillic_fraction(mn_sents[j]) < MIN_CYRILLIC_FRACTION:
                continue
            pairs.append((en_sents[i], mn_sents[j], float(sim[i, j])))

    print(f"Mutual-best-match pairs at >= {THRESHOLD}: {len(pairs)}")
    out = pd.DataFrame(pairs, columns=["english", "mongolian", "labse_sim"])
    out.to_csv(out_path, index=False)
    print(f"Saved to {out_path}")
