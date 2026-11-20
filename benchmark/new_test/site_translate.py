"""Translate new_test EN docx with the website pipeline (webapp/translator.py) and a chosen model.
    MODEL_EN_MN=benchmark/nllb13_v5_quality/final venv/bin/python benchmark/new_test/site_translate.py v5
Writes benchmark/new_test/{A,B}_<tag>.docx (same format the site produces)."""
import os, sys
sys.path.insert(0, "webapp")
import documents, translator  # noqa: E402
tag = sys.argv[1]
for k, f in [("A", "A_general_FLORES_EN.docx"), ("B", "B_policy_Vision2050_EN.docx")]:
    paras = [p for p in documents.read_document(f"benchmark/new_test/{f}", ".docx")[0] if p.strip()]
    out = translator.translate_paragraphs(paras, "en-mn")
    documents.write_docx(out, f"benchmark/new_test/{k}_{tag}.docx")
    print(k, len(out), out[0][:80])
