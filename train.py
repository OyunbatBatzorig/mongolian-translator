"""Fine-tune NLLB-200-distilled-600M for EN->MN translation.

Trains on data/train.csv, validates on data/val.csv, tracks BLEU/chrF per
epoch, and saves the best checkpoint (by validation BLEU) to
./my_finetuned_mn_model. data/test.csv is intentionally not touched here --
use test.py / benchmark/run_eval.py against it for a final, unbiased check.
"""
import evaluate
import numpy as np
import pandas as pd
from datasets import Dataset
from transformers import (
    AutoModelForSeq2SeqLM,
    DataCollatorForSeq2Seq,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

MODEL_NAME = "facebook/nllb-200-distilled-600M"
SRC_LANG = "eng_Latn"
TGT_LANG = "khk_Cyrl"  # NLLB/FLORES-200 code for (Halh) Mongolian; "mon_Cyrl" does not exist in NLLB
MAX_LEN = 128

# AutoTokenizer resolves NLLB to a generic fast backend in this transformers
# version that silently drops the language-prefix token; import the NLLB
# tokenizer class directly to get correct [lang_code, tokens..., </s>] framing.
tokenizer = NllbTokenizer.from_pretrained(MODEL_NAME, src_lang=SRC_LANG, tgt_lang=TGT_LANG)
model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME)
model.generation_config.forced_bos_token_id = tokenizer.convert_tokens_to_ids(TGT_LANG)

train_ds = Dataset.from_pandas(pd.read_csv("data/train.csv"), preserve_index=False)
val_ds = Dataset.from_pandas(pd.read_csv("data/val.csv"), preserve_index=False)


def preprocess(examples):
    return tokenizer(
        text=examples["english"],
        text_target=examples["mongolian"],
        max_length=MAX_LEN,
        truncation=True,
    )


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


training_args = Seq2SeqTrainingArguments(
    output_dir="./mn-translator-results",
    # batch 16 OOMs this 600M model on this GPU (other processes hold ~7.6GB);
    # batch 4 + grad accumulation keeps the same effective batch size of 16.
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    gradient_accumulation_steps=4,
    gradient_checkpointing=True,
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
    report_to="none",
)

trainer = Seq2SeqTrainer(
    model=model,
    args=training_args,
    train_dataset=train_tok,
    eval_dataset=val_tok,
    data_collator=DataCollatorForSeq2Seq(tokenizer, model=model),
    compute_metrics=compute_metrics,
)

print("Starting fine-tuning...")
trainer.train()
trainer.save_model("./my_finetuned_mn_model")
tokenizer.save_pretrained("./my_finetuned_mn_model")
print("Done! Best model (by validation BLEU) saved to 'my_finetuned_mn_model'.")
