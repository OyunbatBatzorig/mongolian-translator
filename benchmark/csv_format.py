"""Standard dataset format used across this project: english,mongolian
always quoted; source,labse_sim always unquoted. Example row:

    "Hello.","Сайн байна уу.",professional,1.0

Used by every data-producing script (pdf_align.py, align_batch.py,
labse_filter.py) and by the one-off reformatting pass over existing files.
"""
COLUMNS = ["english", "mongolian", "source", "labse_sim"]


def _escape(s):
    return str(s).replace('"', '""')


def write_standard_csv(df, path):
    """df must have english, mongolian, source, labse_sim columns."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(COLUMNS) + "\n")
        for row in df.itertuples(index=False):
            en = _escape(row.english)
            mn = _escape(row.mongolian)
            sim = row.labse_sim
            sim_str = "" if sim == "" or sim is None or (isinstance(sim, float) and sim != sim) else f"{float(sim):.6f}"
            f.write(f'"{en}","{mn}",{row.source},{sim_str}\n')
