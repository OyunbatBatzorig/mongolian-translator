"""Extract + LaBSE-align a parallel EN/MN PDF pair (smoke test on the NHRCM
24th report). Runs on CPU by default so it doesn't compete with a training
job for GPU memory.

Usage:
    python benchmark/pdf_align.py <en.pdf> <mn.pdf> <out.csv> [source_name]

Writes english,mongolian,source,labse_sim (see csv_format.py) -- source
name defaults to <out.csv>'s basename if not given.
"""
import re
import sys

import fitz
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer

from csv_format import write_standard_csv

THRESHOLD = 0.75
MIN_WORDS = 4
MAX_CHARS = 500
# Below this fraction of Cyrillic letters, the "Mongolian" side is mostly
# untranslated Latin text -- almost always a citation/reference entry left
# in its original English rather than a real translation. Catches this
# regardless of citation format (Amnesty codes, UN report titles, etc.),
# which narrow per-format regexes can't keep up with.
MIN_CYRILLIC_FRACTION = 0.6


FOOTNOTE_PREFIX = re.compile(r"^\d{1,4}\s*(?=[A-ZА-ЯЁӨҮ])")
# A superscript footnote marker glued directly onto the end of a word, no
# space (e.g. "deaths2." or "cases7, where"). Requires a lowercase letter
# immediately before the digit(s) -- real numbers are separated by a space
# ("Article 19.", "in 2021.") so this doesn't touch them.
FOOTNOTE_SUFFIX = re.compile(r"(?<=[a-zа-яёөү])\d{1,3}(?=[\s.,;:!?]|$)")
# Section/chapter titles are often set as their own bold/all-caps text block
# right before a paragraph, with no punctuation of their own, so they fuse
# onto the start of the next real sentence (e.g. "COMPANY ACCOUNTABILITY
# Company actors attempting to influence..."). Requires 2+ consecutive
# all-caps words so a real sentence starting with a single acronym like
# "UN Human Rights Committee..." is left alone.
HEADING_PREFIX = re.compile(r"^(?:[А-ЯЁӨҮA-Z]{2,}\s+){2,}(?=[А-ЯЁӨҮA-Z][а-яёөүa-z])")
# Orphaned tail of a URL that wrapped across a line break in the PDF (the
# "https://..." prefix gets stripped by URL_PATTERN below, but a fragment
# like "redevelopment-2/" that continued on the next line is left behind
# since it no longer looks like a URL on its own).
URL_WRAP_FRAGMENT = re.compile(r"\b[a-z][a-z0-9-]{3,}-\d+/")
# Citation/bibliography entries, not real prose -- drop the whole sentence
# rather than try to clean it, since the two language editions cite sources
# inconsistently (different report, different page/para) and end up
# matched by accident on shared boilerplate text rather than translation.
CITATION_PATTERN = re.compile(
    r"\((ASA|POL|AFR|AMR|EUR|IOR|MDE|NWS|ACT)\s?\d+/\d+/\d{4}\)"
    r"|\bp\.\s?\d+,?\s*para\.?\s*\d+\b",
    re.IGNORECASE,
)


def cyrillic_fraction(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 1.0
    cyr = sum(1 for c in letters if re.match(r"[а-яёөүА-ЯЁӨҮ]", c))
    return cyr / len(letters)


def extract_sentences(path):
    doc = fitz.open(path)
    text = "\n".join(page.get_text() for page in doc)

    # Drop table-of-contents lines (dot leaders like "....... 177") and
    # lines that are just page-number/whitespace noise from extraction.
    lines = [
        line for line in text.split("\n")
        if not re.search(r"\.{4,}", line) and not re.fullmatch(r"\s*\d+\s*", line)
    ]
    text = " ".join(lines)

    # Strip citation URLs before sentence-splitting. A bare "https://..."
    # starts with a lowercase letter and ends mid-path (no . ! ?), so it
    # never matches a split boundary either side -- left in place it fuses
    # the real sentences before and after it into one bogus "sentence".
    text = re.sub(r"https?://\S+", " ", text)
    text = URL_WRAP_FRAGMENT.sub(" ", text)

    # A footnote marker glued directly onto sentence-ending punctuation with
    # no space (e.g. "discussion.29 However...") sits between the period and
    # the next capital letter, so the split regex below -- which requires
    # whitespace directly after the punctuation -- never fires there. Two
    # real sentences fuse into one with the footnote number stuck in the
    # middle. Strip the digits but keep the punctuation so the boundary
    # becomes visible again.
    text = re.sub(r"(?<=[.!?])\d{1,3}(?=\s+[A-ZА-ЯЁӨҮ])", "", text)

    # Simple sentence split on . ! ? followed by whitespace + capital/digit,
    # works for both English and Mongolian Cyrillic (same punctuation marks).
    raw_sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁӨҮ0-9])", text)

    sentences = []
    for s in raw_sentences:
        s = re.sub(r"\s+", " ", s).strip()
        # Superscript footnote markers flatten into the text stream as a
        # bare leading number (e.g. "273Please see..."); strip once, since
        # the footnote number itself carries no translatable meaning and
        # numbering often differs between the EN/MN editions anyway.
        s = FOOTNOTE_PREFIX.sub("", s).strip()
        s = FOOTNOTE_SUFFIX.sub("", s).strip()
        s = HEADING_PREFIX.sub("", s).strip()
        if CITATION_PATTERN.search(s):
            continue
        if len(s.split()) >= MIN_WORDS and len(s) <= MAX_CHARS:
            sentences.append(s)
    return sentences


def main():
    en_path, mn_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    source = sys.argv[4] if len(sys.argv) > 4 else re.sub(r"_aligned\.csv$", "", out_path.split("/")[-1])

    en_sents = extract_sentences(en_path)
    mn_sents = extract_sentences(mn_path)
    print(f"Extracted {len(en_sents)} EN sentences, {len(mn_sents)} MN sentences")

    model = SentenceTransformer("sentence-transformers/LaBSE", device="cpu")
    en_emb = model.encode(en_sents, batch_size=64, show_progress_bar=True,
                           convert_to_numpy=True, normalize_embeddings=True)
    mn_emb = model.encode(mn_sents, batch_size=64, show_progress_bar=True,
                           convert_to_numpy=True, normalize_embeddings=True)

    sim = en_emb @ mn_emb.T  # [n_en, n_mn] cosine similarity matrix
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
    out["source"] = source
    write_standard_csv(out[["english", "mongolian", "source", "labse_sim"]], out_path)
    print(f"Saved to {out_path}")

    print("\nSample pairs:")
    for row in out.sample(min(8, len(out)), random_state=1).itertuples():
        print(f"  [{row.labse_sim:.3f}] EN: {row.english[:80]}")
        print(f"          MN: {row.mongolian[:80]}")


if __name__ == "__main__":
    main()
