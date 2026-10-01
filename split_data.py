"""Dedupe data/all_pairs_raw.csv and split into train/val/test for fair
evaluation. Grouped by english text so a sentence with multiple valid
Mongolian translations (common in the raw professional pairs) never gets
split across train and val/test -- see "Split correction" in
data/README.md for why this matters.
"""
import sys

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit
from sentence_transformers import SentenceTransformer

sys.path.insert(0, "benchmark")
from csv_format import write_standard_csv

SEED = 42

df = pd.read_csv("data/all_pairs_raw.csv")
before = len(df)
df = df.drop_duplicates(subset=["english", "mongolian"]).reset_index(drop=True)
print(f"Rows: {before} -> {len(df)} after dedupe ({before - len(df)} removed)")

gss1 = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=SEED)
train_idx, temp_idx = next(gss1.split(df, groups=df["english"]))
train_df = df.iloc[train_idx].reset_index(drop=True)
temp_df = df.iloc[temp_idx].reset_index(drop=True)

gss2 = GroupShuffleSplit(n_splits=1, test_size=0.5, random_state=SEED)
val_idx, test_idx = next(gss2.split(temp_df, groups=temp_df["english"]))
val_df = temp_df.iloc[val_idx].reset_index(drop=True)
test_df = temp_df.iloc[test_idx].reset_index(drop=True)

model = SentenceTransformer("sentence-transformers/LaBSE", device="cpu")


def score(d):
    en_emb = model.encode(d["english"].tolist(), batch_size=64, show_progress_bar=False,
                           convert_to_numpy=True, normalize_embeddings=True)
    mn_emb = model.encode(d["mongolian"].tolist(), batch_size=64, show_progress_bar=False,
                           convert_to_numpy=True, normalize_embeddings=True)
    return np.sum(en_emb * mn_emb, axis=1)


for name, d in [("train", train_df), ("val", val_df), ("test", test_df)]:
    d["labse_sim"] = score(d)
    d["source"] = "professional"
    write_standard_csv(d[["english", "mongolian", "source", "labse_sim"]], f"data/{name}.csv")

print(f"train: {len(train_df)}  val: {len(val_df)}  test: {len(test_df)}")
