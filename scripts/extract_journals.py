#!/usr/bin/env python3
"""
Extract journal data from PubMed_Journals_Categorized.xlsx for a given sheet.
Writes a CSV compatible with the womens_health_digest.py pipeline.

Usage:
  python3 scripts/extract_journals.py
"""

import csv
from pathlib import Path
import openpyxl

XLSX_PATH   = Path("/Users/meaganmorris/Downloads/PubMed_Journals_Categorized.xlsx")
SHEET_NAME  = "Womens Health & Menopause"
OUTPUT_DIR  = Path(__file__).parent.parent / "data"
OUTPUT_PATH = OUTPUT_DIR / f"{SHEET_NAME}.csv"

COLUMNS = ["Journal Title", "ISSN (Print)", "ISSN (Online)", "Categories"]


def main():
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb[SHEET_NAME]

    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    col_idx = {name: i for i, name in enumerate(header) if name in COLUMNS}

    missing = [c for c in COLUMNS if c not in col_idx]
    if missing:
        raise ValueError(f"Missing columns in sheet: {missing}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            record = {col: (row[col_idx[col]] or "") for col in COLUMNS}
            for issn_field in ("ISSN (Print)", "ISSN (Online)"):
                val = str(record[issn_field]).strip()
                if val in ("", "None"):
                    record[issn_field] = ""
                else:
                    record[issn_field] = val
            if not str(record["Journal Title"]).strip() or str(record["Journal Title"]) == "None":
                continue
            writer.writerow(record)
            written += 1

    print(f"Wrote {written} journals to {OUTPUT_PATH}")
    wb.close()


if __name__ == "__main__":
    main()
