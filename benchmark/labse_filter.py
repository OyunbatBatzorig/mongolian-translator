"""LaBSE-verify data/opus_filtered.csv: score each EN-MN pair by cosine
similarity of their LaBSE sentence embeddings, keep pairs at/above the
threshold from the project plan (~0.75).
"""
import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer

THRESHOLD = 0.75
BATCH_SIZE = 256

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
print(f"\nBy source, mean similarity and pass rate at {THRESHOLD}:")
print(df.groupby("source")["labse_sim"].agg(["mean", "count"]))
print(df.groupby("source").apply(lambda g: (g["labse_sim"] >= THRESHOLD).mean()))

kept = df[df["labse_sim"] >= THRESHOLD].drop(columns=["labse_sim"])
print(f"\nKept {len(kept)}/{len(df)} pairs (>= {THRESHOLD} similarity)")

kept.to_csv("data/opus_verified.csv", index=False)
print("Saved to data/opus_verified.csv")
