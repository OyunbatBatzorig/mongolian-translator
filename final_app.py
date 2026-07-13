import os

import torch
from transformers import AutoModelForSeq2SeqLM
from transformers.models.nllb.tokenization_nllb import NllbTokenizer

model_path = "./my_finetuned_mn_model"
SRC_LANG = "eng_Latn"
TGT_LANG = "khk_Cyrl"  # NLLB/FLORES-200 code for (Halh) Mongolian

if not os.path.exists(model_path):
    print(f"Error: Final model folder '{model_path}' not found!")
    exit()

print("Loading final fine-tuned model...")
tokenizer = NllbTokenizer.from_pretrained(model_path, src_lang=SRC_LANG, tgt_lang=TGT_LANG)
model = AutoModelForSeq2SeqLM.from_pretrained(model_path)

device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)
model.generation_config.forced_bos_token_id = tokenizer.convert_tokens_to_ids(TGT_LANG)


def translate_to_mongolian(text):
    model_inputs = tokenizer(text, return_tensors="pt").to(device)
    generated_tokens = model.generate(
        **model_inputs,
        max_length=128,
        num_beams=5,  # Improves translation quality
    )
    return tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0]


# Interactive Loop
print("\n--- English to Mongolian Translator Ready ---")
print("Type 'exit' to quit.")

while True:
    user_input = input("\nEnter English text: ")
    if user_input.lower() == "exit":
        break

    translation = translate_to_mongolian(user_input)
    print(f"Mongolian: {translation}")
