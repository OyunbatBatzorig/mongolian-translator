"""Type an English sentence, see both models' Mongolian translation side by
side: the professional-only baseline vs the one trained with added OPUS data.

Usage:
    python benchmark/compare_interactive.py
"""
import torch
from transformers import AutoModelForSeq2SeqLM
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

SRC_LANG = "eng_Latn"
TGT_LANG = "khk_Cyrl"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

MODELS = {
    "baseline (professional only)": "benchmark/nllb/final",
    "expanded (professional + OPUS)": "benchmark/nllb_expanded/final",
}

loaded = {}
for label, path in MODELS.items():
    print(f"Loading {label}...")
    tok = NllbTokenizer.from_pretrained(path, src_lang=SRC_LANG, tgt_lang=TGT_LANG)
    model = AutoModelForSeq2SeqLM.from_pretrained(path).to(DEVICE)
    model.eval()
    model.generation_config.forced_bos_token_id = tok.convert_tokens_to_ids(TGT_LANG)
    loaded[label] = (tok, model)

print("\nReady. Type an English sentence (or 'exit' to quit).\n")

while True:
    text = input("EN: ").strip()
    if text.lower() == "exit":
        break
    if not text:
        continue
    for label, (tok, model) in loaded.items():
        enc = tok(text, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            out = model.generate(**enc, max_length=128, num_beams=4)
        translation = tok.batch_decode(out, skip_special_tokens=True)[0]
        print(f"  [{label}] {translation}")
    print()
