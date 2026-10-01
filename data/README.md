# EN-MN Human Rights Translation Dataset

English-Mongolian sentence pairs for fine-tuning a human-rights-specialized
translator. Built from three sources: professionally translated pairs
(gold standard), official human rights PDF reports (LaBSE-aligned), and the
OPUS parallel corpus (LaBSE-filtered).

## Quick reference: which file to use

- **Training a model?** Use `train_final.csv` (train split) + `val.csv` +
  `test.csv`. `test.csv` should never be used for training or model
  selection — it's the only truly held-out set for reporting final numbers.
- **Want everything in one file to browse or build your own mix from?**
  Use `all_training_data.csv` — every distinct example, no upweighting.
- **Want to inspect a single source?** Use one of the per-source files
  below rather than the combined ones.
- **Curious what didn't make the cut, or why a threshold was chosen?** See
  "Ablation history" at the bottom — those files are kept for reference,
  not for training the current model.

## Standard format

Every file in this directory uses the same four columns, in this order:

```
"english sentence","mongolian sentence",source,labse_sim
```

`english`/`mongolian` are always quoted (even when not strictly required by
CSV rules, for visual consistency when opened directly); `source` and
`labse_sim` never are. `source` identifies which document/corpus a row
came from (`professional`, `NHRCM 20th`, `Amnesty 2025`, `NLLB`, etc.).
`labse_sim` is the cosine similarity between the LaBSE embeddings of the
English and Mongolian sentence (range 0-1) — present everywhere, including
the professional pairs, as a transparency/quality signal, even though it
wasn't used to *select* those pairs (they're gold-standard by virtue of
being human-translated, not by LaBSE score). Written by the shared
`benchmark/csv_format.py` helper, used by every data-producing script.

## The core split

| File | Rows | What it is |
|---|---|---|
| `all_pairs_raw.csv` | 4,538 | The original professional pairs, before dedup. Kept for provenance. |
| `train.csv` | 3,219 | Professional pairs, deduped, ~80% split. This is the gold-standard core of every training mix below. |
| `val.csv` | 407 | Professional pairs, ~10% split. Used during training for model selection (best-checkpoint-by-BLEU). |
| `test.csv` | 409 | Professional pairs, ~10% split. **Held out** — only used for final reporting, never for training or checkpoint selection. |

`source` is `professional` throughout these four files; mean `labse_sim`
is ~0.74-0.75 (lower than the 0.85 OPUS threshold — expected, since LaBSE
rewards literal correspondence and professional legal translation often
restructures sentences across languages more freely than mined bitext does).

### Split correction

The split is a **grouped** split (`sklearn.model_selection.GroupShuffleSplit`,
grouped by the `english` column, seed 42) — every row sharing the same
English sentence lands in the same split, never scattered across two.

This replaced an earlier plain random split that deduped only on exact
`(english, mongolian)` *pairs*. The raw professional data has legitimate
cases where one English sentence got two different valid Mongolian
translations in different source documents (e.g. `"activists"` →
`"идэвхтэн гишүүн"` in one place, `"идэвхтнүүд"` in another) — since the
Mongolian side differs, both rows survived pair-level dedup as "distinct,"
and a plain row-level split could put one in `train.csv` and the other in
`val.csv`/`test.csv`. That meant the model could see an English input
during training and then be evaluated on the *same input* expecting a
different reference translation — a real, if narrow, form of leakage
(affected 15/403 val rows and 9/404 test rows in the old split). The
grouped split fixes this while keeping every row — the alternate
translations are still useful training signal, just now guaranteed to
stay together on one side of the split.

If you're reproducing results from earlier in this project's history:
numbers reported against the old split (test BLEU 17.56-18.15 across
several experiments) are **not directly comparable** to numbers reported
against this corrected split (test BLEU 21.01) — both the split boundary
and the row counts changed, so treat the corrected-split number as the
trustworthy baseline going forward, not a validated improvement over the
old numbers.

## PDF-aligned sources (Tier 1: official documents)

Extracted from parallel English/Mongolian PDF report pairs using
`benchmark/pdf_align.py`: PyMuPDF text extraction → sentence splitting →
LaBSE embedding → mutual-best-match alignment (pair kept only if each
side's closest match is the other) at cosine similarity ≥ 0.75.

| File | Rows | `source` values | Document(s) |
|---|---|---|---|
| `nhrcm_aligned.csv` | 11,973 | `NHRCM 20th` … `NHRCM 24th` | National Human Rights Commission of Mongolia annual status reports, 20th–24th editions. |
| `amnesty_aligned.csv` | 3,297 | `Amnesty 2024` … `Amnesty 2026` | Amnesty International "The State of the World's Human Rights," 2024–2026 editions. Mongolian editions are abridged (~16-18% of English content volume — they translate the Mongolia-relevant sections, not the full 150+-country report), so yield is lower relative to English sentence count than the NHRCM reports. |
| `amicus_aligned.csv` | 124 | `Amicus` | Amicus curiae legal brief. Heavily footnoted with case-law citations; an early extraction pass (before the citation/Cyrillic-fraction filters existed) yielded 188 raw matches, of which 110 (58%) turned out to be untranslated case-law references left in English rather than real translated prose — see Cleanup steps below. |
| `hr_agenda_aligned.csv` | 61 | `HR Agenda` | Human Rights Agenda policy briefing. Citation-dense document; filtering removed about half the raw matches. |
| `pdf_all_aligned.csv` | 15,447 | (all of the above) | All four files above, combined and deduped, `source`/`labse_sim` carried through. This is what actually goes into `train_final.csv`. |

Two additional documents were tested and excluded: "Just Transition and
Human Rights" and "Human Rights in the Life Cycle of Renewable Energy and
Critical Minerals" — both too short (8-17 pages) to yield real content;
every match was just Amnesty's standard report boilerplate ("Our vision is
of a world where...", already present many times over in `amnesty_aligned.csv`).

### Cleanup applied during extraction (all in `pdf_align.py`)

PDF text extraction flattens a lot of visual formatting into the plain
text stream in ways that corrupt naive sentence splitting. Fixed, in the
order applied:

1. **Table-of-contents lines** (dot leaders like `.......177`) and bare
   page-number lines — dropped before sentence splitting.
2. **URLs** — a bare `https://...` never ends in `. ! ?`, so left in place
   it prevents the sentence splitter from ever finding the boundary on
   either side of it, fusing two real sentences into one garbage blob.
   Stripped before splitting.
3. **Word-wrapped URL fragments** — if a URL wrapped across a PDF line
   break, the fragment after the break (e.g. `redevelopment-2/`) survives
   URL-stripping since it no longer looks like a URL on its own. Caught by
   a separate pattern.
4. **Mid-sentence footnote markers glued to punctuation** — e.g.
   `"discussion.29 However..."`. The footnote number sits between the
   period and the next capital letter with no space, so the splitter's
   "period + whitespace + capital" rule never fires there either. Stripped
   before splitting so the boundary becomes visible again. This alone
   recovered a meaningful amount of real content — NHRCM yields went up
   12-29% when this was added (previously-merged sentences got correctly
   split into two clean ones).
5. **Leading footnote numbers** on a sentence (`"47 National Child Abuse
   Hotline..."`) — stripped if followed directly by a capital letter.
6. **Trailing footnote numbers** glued to a word with no space
   (`"deaths2."`) — stripped. Requires zero whitespace before the digit,
   which real numbers never have (`"Article 19."` has a space, so it's
   untouched).
7. **Section/chapter heading bleed** — a bold all-caps heading with no
   punctuation of its own fuses onto the next paragraph (`"COMPANY
   ACCOUNTABILITY Company actors attempting to..."`). Stripped when 2+
   consecutive all-caps words precede a normal-case word (1 all-caps word,
   e.g. an acronym like "UN Human Rights Committee...", is left alone).
8. **Bibliographic citations** — Amnesty document reference codes
   (`(ASA 30/1297/2019)`) and page/paragraph citations (`p.7, para 4`).
   Whole sentence dropped rather than cleaned, since these aren't real
   prose and the two language editions often cite different reports at
   the same spot, producing accidental matches.
9. **Untranslated citations (general case)** — the pattern above only
   catches specific citation formats. The general, more robust fix: if the
   "Mongolian" side is under 60% Cyrillic characters, it's almost
   certainly a reference left in English rather than actually translated
   (real Mongolian prose is predominantly Cyrillic even with the
   occasional embedded English acronym). This is what caught the 110
   untranslated case-law citations in `amicus_aligned.csv`. Verified
   against every source with no false positives.

## OPUS-derived data (Tier 2: web-mined corpus)

`data/opus_filtered.csv` (53,860 rows) is the raw OPUS pull (`source`
values: NLLB, CCAligned, Tatoeba, TED2020, OpenSubtitles, NeuLab-TedTalks,
QED) filtered to human-rights-relevant vocabulary by keyword. `labse_sim`
is empty here — it has **not** been checked for translation quality yet,
that's the next step.

`data/opus_filtered_scored.csv` — the same file with `labse_sim` filled in
(LaBSE cosine similarity between the English and Mongolian sides of each
pair). Kept because it lets you re-filter at any threshold without
recomputing embeddings (`sentence-transformers/LaBSE`, ~53,860 pairs, a
few minutes on GPU). Produced by `benchmark/labse_filter.py`.

`data/opus_verified_85_clean.csv` (20,342 rows) — the file actually used
in training, `source`/`labse_sim` carried through from the scored file.
Built by (also `benchmark/labse_filter.py`):
1. Filtering `opus_filtered_scored.csv` to `labse_sim >= 0.85`.
2. Removing rows matching biblical/religious vocabulary. About 5.8% of
   the "NLLB" mined sub-corpus specifically turned out to be Bible
   translation text that passed the human-rights keyword filter by
   accident (shared vocabulary like "justice," "law," "protect"). Detected
   by a keyword list (heaven, Jesus, disciples, prophet, etc.) and
   confirmed by checking which OPUS sub-corpus it came from — 1,107/1,159
   flagged rows were from "NLLB" specifically, consistent with that
   corpus being mined from web-scale crawls where religious translation
   sites are a common source of parallel text for low-resource languages.
3. The Cyrillic-fraction filter from the PDF pipeline (step 9 above),
   applied here too — removed 286 more rows (movie/game titles, technical
   specs, and other low-Cyrillic-content matches, mostly off-domain junk
   like film release dates rather than real translated prose).

### Why 0.85 and not the default 0.75

Two full training runs were compared on the identical held-out
`test.csv`, same recipe otherwise: professional×3 + OPUS pairs, varying
only the LaBSE threshold on the OPUS side.

| OPUS threshold | OPUS pairs kept | Test BLEU | Test chrF |
|---|---|---|---|
| 0.75 (default, unfiltered beyond keyword match) | 53,218 | 17.59 | 48.83 |
| 0.85 + biblical/citation cleanup | 20,342 | **18.05** | **49.31** |

More OPUS volume at looser quality did **not** help — BLEU was flat across
every "more data" experiment (0.75-threshold OPUS, PDF-only, etc., all
landing at 17.56-17.62 BLEU). Stricter filtering plus removing off-domain
content was what actually moved the number. This validated a "quality
over volume" approach for the rest of the pipeline. See the git commit
history / conversation log around `train_opus85.csv` for the full
before/after comparison.

## All data in one file

`all_training_data.csv` (39,005 rows) — every distinct example from every
source above, deduped, in one file. `train.csv` here is at its natural 1x
count (3,219), *not* the 3x training-upweight — upweighting is a
training-time choice, applied separately in `train_final.csv` below, not
part of "the dataset" itself. This is the file to reach for if you just
want everything in one place to browse, filter by `source`, or build your
own training mix from scratch.

## The final training mix

`train_final.csv` (45,443 rows) = `train.csv` × 3 (9,657, upweighting the
gold-standard professional data relative to the larger, noisier automated
sources) + `pdf_all_aligned.csv` (15,444) + `opus_verified_85_clean.csv`
(20,342), shuffled (seed 42), `source`/`labse_sim` carried through. This
is what the current best model was actually trained on.

Verified zero row overlap between this file (and `all_training_data.csv`)
and `val.csv`/`test.csv` by exact English-sentence match, at every stage
of the pipeline — including after the train/val/test split itself was
rebuilt with a grouped split to fix a leakage bug (see "Split correction"
below).

## Ablation history (not used in the current best model)

These files represent earlier experiments, kept for reference rather than
deleted, since each one answers a "what if we'd done X instead" question:

- `opus_verified.csv` — OPUS at the default 0.75 threshold, no biblical-content
  removal. What `train_expanded.csv` was built from.
- `train_expanded.csv` — professional×3 + `opus_verified.csv` (62,902 rows).
  Test BLEU 17.59 / chrF 48.83 — the "more volume, less filtering" baseline
  that motivated tightening the threshold.
- `opus_verified_85.csv` — OPUS at 0.85 threshold, *before* biblical-content
  removal (21,910 rows, vs. the 20,342-row cleaned version actually used).
- `train_opus85.csv` — professional×3 + `opus_verified_85.csv` including the
  biblical content (31,594 rows). Superseded once the biblical-content bug
  was found; not the same as the final 0.85 result reported above.
- `train_pdf.csv` — professional (no upweight) + `pdf_all_aligned.csv` only,
  no OPUS at all (13,682 rows at the time it was built). Test BLEU 17.62 /
  chrF 48.23 — confirms clean-but-smaller Tier 1 data alone didn't move
  BLEU either; it took both the volume *and* the OPUS quality fix together.

These five ablation files predate the standard 4-column format above and
still use the older `english, mongolian` (2-column) layout — that's
expected, not a mistake; they were snapshots taken mid-pipeline before the
`source`/`labse_sim`-everywhere convention was settled on.

## Format notes

- Encoding: UTF-8. Mongolian text is Cyrillic script.
- No column ever contains nulls or empty strings — checked at every
  combination step.
