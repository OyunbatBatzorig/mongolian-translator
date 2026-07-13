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

    train_ds = to_dataset("data/train.csv")
    val_ds = to_dataset("data/val.csv")

    def preprocess(examples):
        model_inputs = tokenizer(
            text=examples["english"],
            text_target=examples["mongolian"],
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

    args = Seq2SeqTrainingArguments(
        output_dir=out_dir,
        per_device_train_batch_size=cfg["batch_size"],
        per_device_eval_batch_size=cfg["batch_size"],
        gradient_accumulation_steps=cfg["grad_accum"],
        num_train_epochs=5,
        learning_rate=5e-5,
        fp16=True,
        logging_steps=20,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=1,
        load_best_model_at_end=True,
        metric_for_best_model="bleu",
        greater_is_better=True,
        predict_with_generate=True,
        generation_max_length=MAX_LEN,
        generation_num_beams=4,
        dataloader_num_workers=2,
        optim="adamw_torch_fused",
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
