"""Interactive CLI to test a trained checkpoint.

Usage:
    python test.py                                  # loads ./my_finetuned_mn_model
    python test.py ./mn-translator-results/checkpoint-500
"""
import os
import sys

import torch
from transformers import AutoModelForSeq2SeqLM
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

SRC_LANG = "eng_Latn"
TGT_LANG = "khk_Cyrl"  # NLLB/FLORES-200 code for (Halh) Mongolian

model_path = sys.argv[1] if len(sys.argv) > 1 else "./my_finetuned_mn_model"

if not os.path.exists(model_path):
    print(f"ERROR: Could not find the folder at {model_path}")
else:
    print(f"Loading checkpoint: {model_path}")

    tokenizer = NllbTokenizer.from_pretrained(model_path, src_lang=SRC_LANG, tgt_lang=TGT_LANG)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.generation_config.forced_bos_token_id = tokenizer.convert_tokens_to_ids(TGT_LANG)

    def translate(text):
        encoded = tokenizer(text, return_tensors="pt").to(device)
        generated_tokens = model.generate(**encoded, max_length=128, num_beams=4)
        return tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0]

    print(f"Result: {translate('Hello, how are you?')}")
