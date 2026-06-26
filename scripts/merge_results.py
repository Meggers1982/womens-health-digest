#!/usr/bin/env python3
"""
Merge all per-job result artifacts into data/results.json.
Run by the deploy job after all matrix jobs complete.

Reads from /tmp/artifacts/wh-results-*/results.json
Writes to data/results.json (appends new studies, deduplicates by PMID)
"""

import json
import os
import requests
from datetime import datetime, timezone
from pathlib import Path

RESEND_KEY    = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL    = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
RECIPIENT     = os.environ.get("RECIPIENT_EMAIL", "meagan.lea.morris@gmail.com")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

ARTIFACTS_DIR = Path("/tmp/artifacts")
OUTPUT_PATH   = Path("data/results.json")

def main():
    # Load existing results
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text())
    else:
        existing = {"last_updated": "", "total_studies": 0, "studies": []}

    # Index existing studies by PMID
    existing_pmids = {s["pmid"]: i for i, s in enumerate(existing["studies"])}
    studies = existing["studies"]
    new_count = 0

    # Find all artifact result files
    artifact_files = sorted(ARTIFACTS_DIR.rglob("results.json"))
    print(f"Found {len(artifact_files)} artifact file(s)")

    for artifact_file in artifact_files:
        try:
            payload = json.loads(artifact_file.read_text())
        except Exception as e:
            print(f"  Skipping {artifact_file}: {e}")
            continue

        run_date  = payload.get("run_date", "")
        category  = payload.get("category", "")
        chunk     = payload.get("chunk", "1/1")

        for study in payload.get("studies", []):
            pmid = study.get("pmid", "")
            if not pmid:
                continue

            # Enrich with run metadata
            study["run_date"]  = run_date
            study["category"]  = category
            study["chunk"]     = chunk
            study.setdefault("status", "new")  # new | pitched | passed | saved

            if pmid in existing_pmids:
                # Update run_date if this is a fresher result, preserve user status
                idx = existing_pmids[pmid]
                existing_status = studies[idx].get("status", "new")
                studies[idx] = study
                studies[idx]["status"] = existing_status
            else:
                studies.append(study)
                existing_pmids[pmid] = len(studies) - 1
                new_count += 1

    # Sort by run_date desc, then pubdate desc
    studies.sort(key=lambda s: (s.get("run_date", ""), s.get("pubdate", "")), reverse=True)

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_studies": len(studies),
        "new_this_run": new_count,
        "studies": studies,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {len(studies)} studies ({new_count} new) to {OUTPUT_PATH}")

    send_summary_email(new_count, len(studies), output["last_updated"])



if __name__ == "__main__":
    main()
