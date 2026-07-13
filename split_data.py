"""Dedupe data/all_pairs_raw.csv and split into train/val/test for fair evaluation."""
import pandas as pd
from sklearn.model_selection import train_test_split

SEED = 42

df = pd.read_csv("data/all_pairs_raw.csv")
before = len(df)
df = df.drop_duplicates(subset=["english", "mongolian"]).reset_index(drop=True)
print(f"Rows: {before} -> {len(df)} after dedupe ({before - len(df)} removed)")

train_df, temp_df = train_test_split(df, test_size=0.2, random_state=SEED)
val_df, test_df = train_test_split(temp_df, test_size=0.5, random_state=SEED)

train_df.to_csv("data/train.csv", index=False)
val_df.to_csv("data/val.csv", index=False)
test_df.to_csv("data/test.csv", index=False)

print(f"train: {len(train_df)}  val: {len(val_df)}  test: {len(test_df)}")
