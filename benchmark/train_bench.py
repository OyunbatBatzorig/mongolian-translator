"""Fine-tune either M2M100-418M or NLLB-200-distilled-600M on the same
train/val split and track BLEU per epoch, for a like-for-like comparison.

Usage:
    python benchmark/train_bench.py m2m100
    python benchmark/train_bench.py nllb
"""
import sys
import json
import time

import evaluate
import numpy as np
import pandas as pd
from datasets import Dataset
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)

MAX_LEN = 128

CONFIGS = {
    "m2m100": dict(
        model_name="facebook/m2m100_418M",
        src_lang="en",
        tgt_lang="mn",
        batch_size=16,
        grad_accum=1,
        grad_checkpointing=False,
        train_csv="data/train.csv",
        num_epochs=5,
        eval_strategy="epoch",
        eval_batch_size=16,
    ),
    "nllb": dict(
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",  # NLLB/FLORES-200 code for (Halh) Mongolian; "mon_Cyrl" does not exist in NLLB
        # 600M model OOM'd at batch 16 and batch 8 alongside other GPU tenants
        # permanently holding ~7.6GB on this box; drop batch size further and
        # use gradient checkpointing, compensating with grad accumulation for
        # the same effective batch size of 16.
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        train_csv="data/train.csv",
        num_epochs=5,
        eval_strategy="epoch",
        eval_batch_size=16,
    ),
    "nllb_expanded": dict(
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        # professional pairs upweighted 3x + LaBSE-verified OPUS pairs (see
        # benchmark/labse_filter.py); ~19.5x the row count of "nllb" above,
        # so fewer epochs and step-based (not epoch-based) eval/checkpointing.
        train_csv="data/train_expanded.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=500,
        eval_batch_size=16,
    ),
    "nllb_pdf": dict(
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        # professional pairs (no upweight, PDF data isn't large enough to
        # dilute them) + LaBSE-aligned pairs from the NHRCM 24th report PDF
        # (see benchmark/pdf_align.py). Same recipe as "nllb" baseline
        # (epoch-based eval) since row count is close in scale.
        train_csv="data/train_pdf.csv",
        num_epochs=5,
        eval_strategy="epoch",
        eval_batch_size=16,
    ),
    "nllb_opus85": dict(
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        # Same recipe as "nllb_expanded" (professional x3 + OPUS), but OPUS
        # filtered at LaBSE similarity >= 0.85 instead of 0.75 (21,910 pairs
        # instead of 53,218) to isolate whether the flat BLEU result was
        # about noisy OPUS data quality rather than volume.
        train_csv="data/train_opus85.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=500,
        eval_batch_size=16,
    ),
    # 2026-11-08: хүний эрх + хууль + ерөнхий (албан ёсны мэдээ/мэдээлэл) өгөгдөл.
    # data/train_v3_general.csv = train_final_nllb_v2_laws_115212 + data/general_clean.csv.
    # 600M ба 1.3B-ийн аль нь сайн гарсан дээр нь benchmark/after_general.sh сонгож ажиллуулна.
    "nllb_v3_general": dict(
        model_name="facebook/nllb-200-distilled-600M", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True,
        train_csv="data/train_v3_general.csv", num_epochs=2,
        eval_strategy="steps", eval_steps=1000, eval_batch_size=16,
    ),
    "nllb13_mn_en_v3": dict(
        # 2026-11-11: MN->EN, continue from nllb13_mn_en_v2 on human pairs of train_v9_ot (adds OT 60k + pubs; no BT)
        model_name="benchmark/nllb13_mn_en_v2/final", src_lang="khk_Cyrl", tgt_lang="eng_Latn", reverse=True,
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=3e-5,
        train_csv="data/train_mn_en_v3.csv", num_epochs=1,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v9_ot": dict(
        # 2026-11-11: continue from v8 on v8 data + all 60k Oyu Tolgoi + 1k UN/UNFPA pubs, human-rights data x3 (user)
        model_name="benchmark/nllb13_v8_wb/final", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=3e-5,
        train_csv="data/train_v9_ot.csv", num_epochs=1,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v8_wb": dict(
        # 2026-11-10: continue from v6 on v6 data + 35k World Bank / user reports (human translations)
        model_name="benchmark/nllb13_v6_bt/final", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=3e-5,
        train_csv="data/train_v8_wb.csv", num_epochs=1,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_mn_en_v2": dict(
        # 2026-11-10: MN->EN 1.3B on all HUMAN pairs of train_v8_wb (no back-translation: its English is synthetic),
        # test_laws / test_vision2050 excluded like nllb_mn_en so the two are comparable.
        model_name="facebook/nllb-200-distilled-1.3B", src_lang="khk_Cyrl", tgt_lang="eng_Latn", reverse=True,
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True,
        train_csv="data/train_mn_en_v2.csv", num_epochs=2,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v7_bt2": dict(
        # 2026-11-10: continue from v6 on v6 data + 75k more back-translation (round 2, nllb_mn_en)
        model_name="benchmark/nllb13_v6_bt/final", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=3e-5,
        train_csv="data/train_v7_bt2.csv", num_epochs=1,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v6_bt": dict(
        # 2026-11-09: continue from nllb13_v5_quality/final on v5 data + 100k back-translated pairs
        # (real Mongolian target, synthetic English source from nllb_mn_en). User: train without asking.
        model_name="benchmark/nllb13_v5_quality/final", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=3e-5,
        train_csv="data/train_v6_bt.csv", num_epochs=1,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v5_quality": dict(
        # 2026-11-09: v4 - strong number mismatches + UN/UNDP/UNFPA reports (strict) + bilingual web pages
        model_name="facebook/nllb-200-distilled-1.3B", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True,
        train_csv="data/train_v5_quality.csv", num_epochs=2,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v4_quality": dict(
        # 2026-11-09: data/train_v4_quality.csv (law>=0.75, talk>=0.80, NLLB top 40k, prof manual clean, HR x2)
        model_name="facebook/nllb-200-distilled-1.3B", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True,
        train_csv="data/train_v4_quality.csv", num_epochs=2,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v3_general": dict(
        model_name="facebook/nllb-200-distilled-1.3B", src_lang="eng_Latn", tgt_lang="khk_Cyrl",
        batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True,
        train_csv="data/train_v3_general.csv", num_epochs=2,
        eval_strategy="steps", eval_steps=1500, eval_batch_size=8,
    ),
    "nllb13_v2": dict(
        # 1.3B параметртэй NLLB (600M-ийн 2 дахин том), EN->MN, nllb_v2_laws-тай ижил өгөгдөл.
        # 24GB GPU-д багтаахын тулд: Adafactor (Adam-аас 2-3 дахин бага санах ой), bf16, gradient checkpointing.
        model_name="facebook/nllb-200-distilled-1.3B",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        optim="adafactor",
        bf16=True,
        train_csv="data/backup_2026-11-08/train_final_nllb_v2_laws_115212.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=1000,
        eval_batch_size=8,
    ),
    "nllb_mn_en": dict(
        # Монгол -> Англи (2026-11-08). Өгөгдөл: train_final.csv-ээс 6 хууль (data/test_laws.csv)
        # болон Vision 2050 (data/test_vision2050.csv)-ийг тусад нь шалгах зорилгоор хассан = train_mn_en.csv.
        # val.csv / test.csv нь ижил, зөвхөн чиглэл нь эсрэг.
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="khk_Cyrl",
        tgt_lang="eng_Latn",
        reverse=True,
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        train_csv="data/train_mn_en.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=1000,
        eval_batch_size=16,
    ),
    "nllb_v2_laws": dict(
        # Same recipe as nllb_final, on the rebuilt train_final.csv (2026-11-08):
        # + 69,783 legal-law clause pairs + 362 AIM pairs + cleaned NHRCM
        # = 115,212 rows. Previous train_final.csv is in data/backup_2026-11-08/.
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        train_csv="data/train_final.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=1000,
        eval_batch_size=16,
    ),
    "nllb_final": dict(
        model_name="facebook/nllb-200-distilled-600M",
        src_lang="eng_Latn",
        tgt_lang="khk_Cyrl",
        batch_size=4,
        grad_accum=4,
        grad_checkpointing=True,
        # Everything that has shown a real gain so far: professional x3 +
        # all 10 PDF-aligned documents (13,309) + OPUS filtered at LaBSE
        # similarity >= 0.85 with biblical/religious content removed
        # (20,628) = 43,621 rows total.
        train_csv="data/train_final.csv",
        num_epochs=2,
        eval_strategy="steps",
        eval_steps=500,
        eval_batch_size=16,
    ),
}


def load_tokenizer(cfg):
    if cfg["model_name"].startswith("facebook/nllb"):
        # AutoTokenizer resolves NLLB to a generic fast backend in this
        # transformers version that ignores src_lang/tgt_lang and drops the
        # language-prefix token. Import the NLLB tokenizer class directly
        # to get correct [lang_code, tokens..., </s>] formatting.
        from transformers.models.nllb.tokenization_nllb import NllbTokenizer
        return NllbTokenizer.from_pretrained(
            cfg["model_name"], src_lang=cfg["src_lang"], tgt_lang=cfg["tgt_lang"]
        )
    return AutoTokenizer.from_pretrained(
        cfg["model_name"], src_lang=cfg["src_lang"], tgt_lang=cfg["tgt_lang"]
    )


def forced_bos_id(tokenizer, cfg):
    if hasattr(tokenizer, "get_lang_id"):
        return tokenizer.get_lang_id(cfg["tgt_lang"])
    return tokenizer.convert_tokens_to_ids(cfg["tgt_lang"])


def main(key, resume_from_checkpoint=None):
    cfg = CONFIGS[key]
    out_dir = f"benchmark/{key}"

    tokenizer = load_tokenizer(cfg)
    model = AutoModelForSeq2SeqLM.from_pretrained(cfg["model_name"])
    model.generation_config.forced_bos_token_id = forced_bos_id(tokenizer, cfg)

    def to_dataset(path):
        df = pd.read_csv(path)
        return Dataset.from_pandas(df, preserve_index=False)

    train_ds = to_dataset(cfg["train_csv"])
    val_ds = to_dataset("data/val.csv")

    def preprocess(examples):
        # "reverse": True бол Монгол -> Англи чиглэлд сургана (баганыг солино)
        src_col, tgt_col = ("mongolian", "english") if cfg.get("reverse") else ("english", "mongolian")
        model_inputs = tokenizer(
            text=examples[src_col],
            text_target=examples[tgt_col],
            max_length=MAX_LEN,
            truncation=True,
        )
        return model_inputs

    train_tok = train_ds.map(preprocess, batched=True, remove_columns=train_ds.column_names)
    val_tok = val_ds.map(preprocess, batched=True, remove_columns=val_ds.column_names)

    bleu = evaluate.load("sacrebleu")
    chrf = evaluate.load("chrf")

    def compute_metrics(eval_preds):
        preds, labels = eval_preds
        if isinstance(preds, tuple):
            preds = preds[0]
        preds = np.where(preds != -100, preds, tokenizer.pad_token_id)
        labels = np.where(labels != -100, labels, tokenizer.pad_token_id)
        decoded_preds = tokenizer.batch_decode(preds, skip_special_tokens=True)
        decoded_labels = tokenizer.batch_decode(labels, skip_special_tokens=True)
        bleu_score = bleu.compute(predictions=decoded_preds, references=[[l] for l in decoded_labels])
        chrf_score = chrf.compute(predictions=decoded_preds, references=[[l] for l in decoded_labels])
        return {"bleu": bleu_score["score"], "chrf": chrf_score["score"]}

    step_based = cfg["eval_strategy"] == "steps"
    args = Seq2SeqTrainingArguments(
        output_dir=out_dir,
        per_device_train_batch_size=cfg["batch_size"],
        per_device_eval_batch_size=cfg["eval_batch_size"],
        gradient_accumulation_steps=cfg["grad_accum"],
        num_train_epochs=cfg["num_epochs"],
        learning_rate=cfg.get("lr", 5e-5),
        fp16=not cfg.get("bf16", False),
        bf16=cfg.get("bf16", False),
        logging_steps=20,
        eval_strategy=cfg["eval_strategy"],
        eval_steps=cfg.get("eval_steps") if step_based else None,
        save_strategy=cfg["eval_strategy"],
        save_steps=cfg.get("eval_steps") if step_based else None,
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="bleu",
        greater_is_better=True,
        predict_with_generate=True,
        generation_max_length=MAX_LEN,
        generation_num_beams=4,
        dataloader_num_workers=2,
        optim=cfg.get("optim", "adamw_torch_fused"),
        gradient_checkpointing=cfg["grad_checkpointing"],
        report_to="none",
    )

    trainer = Seq2SeqTrainer(
        model=model,
        args=args,
        train_dataset=train_tok,
        eval_dataset=val_tok,
        data_collator=DataCollatorForSeq2Seq(tokenizer, model=model),
        compute_metrics=compute_metrics,
    )

    start = time.time()
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    elapsed = time.time() - start

    trainer.save_model(f"{out_dir}/final")
    tokenizer.save_pretrained(f"{out_dir}/final")

    history = trainer.state.log_history
    with open(f"{out_dir}/train_history.json", "w") as f:
        json.dump({"elapsed_seconds": elapsed, "log_history": history}, f, indent=2)

    print(f"\n=== {key} done in {elapsed/60:.1f} min ===")
    eval_entries = [h for h in history if "eval_bleu" in h]
    if eval_entries:
        best = max(eval_entries, key=lambda h: h["eval_bleu"])
        print(f"Best val BLEU: {best['eval_bleu']:.2f} (epoch {best.get('epoch')})")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in CONFIGS:
        print(f"Usage: python {sys.argv[0]} [{'|'.join(CONFIGS)}] [resume_checkpoint_dir]")
        sys.exit(1)
    resume = sys.argv[2] if len(sys.argv) == 3 else None
    main(sys.argv[1], resume_from_checkpoint=resume)
