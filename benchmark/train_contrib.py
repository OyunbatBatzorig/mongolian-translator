"""Админ самбараас эхлүүлсэн сургалт: одоогийн загвараас үргэлжлүүлж (lr 2e-5, 1 эпох) шинэ + давтлагын дата дээр сургана.
    venv/bin/python benchmark/train_contrib.py <run> <en-mn|mn-en> <base model dir> <train csv>"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_bench as tb
run, direction, base, csv = sys.argv[1:5]
key = f"contrib_runs/{run}/{direction}"
src, tgt = ("eng_Latn", "khk_Cyrl") if direction == "en-mn" else ("khk_Cyrl", "eng_Latn")
tb.CONFIGS[key] = dict(model_name=base, src_lang=src, tgt_lang=tgt, reverse=(direction == "mn-en"),
                       batch_size=4, grad_accum=4, grad_checkpointing=True, optim="adafactor", bf16=True, lr=2e-5,
                       train_csv=csv, num_epochs=1, eval_strategy="steps", eval_steps=1000, eval_batch_size=8)
# 2026-11-15: сервер унтарсан / тасарсан бол сүүлийн бүрэн хадгалсан цэгээс (checkpoint) үргэлжлүүлнэ
import glob, json
resume = None
for ck in sorted(glob.glob(f"benchmark/{key}/checkpoint-*"), key=lambda p: int(p.rsplit("-", 1)[-1]), reverse=True):
    try:
        st = json.load(open(os.path.join(ck, "trainer_state.json")))   # бүрэн хадгалагдсан эсэх
        if os.path.exists(os.path.join(ck, "optimizer.pt")) and not os.path.isdir(f"benchmark/{key}/final"):
            resume = ck
            print(f"resuming from {ck} (step {st.get('global_step')})", flush=True)
            break
    except (OSError, ValueError):
        continue
tb.main(key, resume_from_checkpoint=resume)
