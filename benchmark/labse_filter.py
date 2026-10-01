"""LaBSE-verify data/opus_filtered.csv: score each EN-MN pair by cosine
similarity of their LaBSE sentence embeddings, keep pairs at/above
THRESHOLD, and drop off-domain biblical/religious content that slips
through the human-rights keyword filter (see data/README.md for why
0.85 + this filter, not the naive 0.75 default).
"""
import re
import sys

import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer

sys.path.insert(0, "benchmark")
from csv_format import write_standard_csv

THRESHOLD = 0.85
BATCH_SIZE = 256
MIN_CYRILLIC_FRACTION = 0.6
BIBLICAL_TERMS = (
    r"\b(heaven|the Lord|Jesus|God|disciples|Israelites|prophet|scripture|"
    r"gospel|sin|righteous|salvation|holy spirit|Christ|apostle|Moses|"
    r"Pharaoh|Egypt(ians)?|tribe of Israel)\b"
)


def cyrillic_fraction(s):
    letters = [c for c in str(s) if c.isalpha()]
    if not letters:
        return 1.0
    cyr = sum(1 for c in letters if re.match(r"[а-яёөүА-ЯЁӨҮ]", c))
    return cyr / len(letters)


df = pd.read_csv("data/opus_filtered.csv")
print(f"Loaded {len(df)} OPUS pairs")

device = "cuda" if torch.cuda.is_available() else "cpu"
model = SentenceTransformer("sentence-transformers/LaBSE", device=device)

en_emb = model.encode(df["english"].tolist(), batch_size=BATCH_SIZE, show_progress_bar=True,
                       convert_to_numpy=True, normalize_embeddings=True)
mn_emb = model.encode(df["mongolian"].tolist(), batch_size=BATCH_SIZE, show_progress_bar=True,
                       convert_to_numpy=True, normalize_embeddings=True)

sims = np.sum(en_emb * mn_emb, axis=1)  # cosine sim, both sides already normalized
df["labse_sim"] = sims

print(f"\nSimilarity distribution:\n{df['labse_sim'].describe()}")
write_standard_csv(df[["english", "mongolian", "source", "labse_sim"]], "data/opus_filtered_scored.csv")
print("Saved full scored set to data/opus_filtered_scored.csv")

kept = df[df["labse_sim"] >= THRESHOLD]
print(f"\nAt >= {THRESHOLD} similarity: {len(kept)}/{len(df)} pairs")

biblical = kept["english"].str.contains(BIBLICAL_TERMS, case=False, regex=True)
print(f"Removing {biblical.sum()} rows matching biblical/religious vocabulary")
kept = kept[~biblical]

cyr_ok = kept["mongolian"].apply(cyrillic_fraction) >= MIN_CYRILLIC_FRACTION
print(f"Removing {(~cyr_ok).sum()} rows under {MIN_CYRILLIC_FRACTION} Cyrillic fraction (untranslated citations, off-domain junk)")
kept = kept[cyr_ok]

print(f"\nFinal: {len(kept)}/{len(df)} pairs")
write_standard_csv(kept[["english", "mongolian", "source", "labse_sim"]], "data/opus_verified_85_clean.csv")
print("Saved to data/opus_verified_85_clean.csv")
