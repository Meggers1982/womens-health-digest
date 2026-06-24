#!/usr/bin/env python3
"""
Mental Health & Brain Science Research Digest
Searches PubMed journals, filters via SERPAPI Google News, writes digest with Claude, sends via Resend.

Required environment variables:
  ANTHROPIC_API_KEY   — Anthropic API key
  SERPAPI_KEY         — SerpAPI key
  RESEND_API_KEY      — Resend API key
  FROM_EMAIL          — Verified sender address in Resend (e.g. digest@yourdomain.com)

Optional environment variables:
  RECIPIENT_EMAIL     — Override recipient (default: meagan.lea.morris@gmail.com)
  CATEGORIES          — Comma-separated category names or "all" (default: all)
  TOPIC_FOCUS         — Optional topic to narrow the search (e.g. "adolescent depression")
"""

import csv
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_KEY      = os.environ["ANTHROPIC_API_KEY"]
SERPAPI_KEY        = os.environ.get("SERPAPI_KEY", "")
RESEND_KEY         = os.environ["RESEND_API_KEY"]
FROM_EMAIL         = os.environ["FROM_EMAIL"]
RECIPIENT          = os.environ.get("RECIPIENT_EMAIL", "meagan.lea.morris@gmail.com")
CATEGORIES_INPUT   = os.environ.get("CATEGORIES", "all")
TOPIC_FOCUS        = os.environ.get("TOPIC_FOCUS", "").strip()
CHUNK_INDEX        = int(os.environ.get("CHUNK_INDEX", "1"))   # 1-based
CHUNK_TOTAL        = int(os.environ.get("CHUNK_TOTAL", "1"))

CSV_PATH = Path(__file__).parent.parent / "data" / "Mental Health - Brain Mental Health.csv"

ALL_CATEGORIES = [
    "Psychology",
    "Psychiatry",
    "Behavioral Sciences",
    "Brain",
    "Psychophysiology",
    "Neurology",
    "Psychopharmacology",
    "Social Sciences",
    "Substance-Related Disorders",
]

DAYS_BACK         = 30
MEDIA_THRESHOLD   = 3    # skip if >= this many Google News results
MEDIA_RELAX       = 5    # fallback threshold if < MIN_STUDIES pass
MIN_STUDIES       = 5    # minimum studies before relaxing media filter
MAX_CANDIDATES    = 30   # max PMIDs carried into SERPAPI screening
PUBMED_ISSN_BATCH = 3    # ISSNs per esearch call (URL length limit)
ESUMMARY_BATCH    = 20   # PMIDs per esummary call
EFETCH_BATCH      = 1    # PMIDs per efetch call (one at a time for reliable parsing)
NCBI_DELAY        = 0.4  # seconds between NCBI calls (max 3 req/sec)
SERPAPI_DELAY     = 1.0  # seconds between SERPAPI calls

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SERPAPI_URL = "https://serpapi.com/search.json"
RESEND_URL  = "https://api.resend.com/emails"

# Title keywords that signal a potentially groundbreaking study
GROUNDBREAKING_SIGNALS = [
    "first", "novel", "unexpected", "contrary", "paradox", "no evidence",
    "challenges", "reverses", "debunks", "replication failure", "previously unknown",
    "newly identified", "overturns", "failed to replicate", "against prior",
    "first randomized", "first longitudinal", "first human", "first study",
    "surpris", "counterintuitive", "revised understanding", "new mechanism",
    "no significant", "opposite", "protective effect",
]

# Publication types to skip at title-screen stage
SKIP_PUBTYPES = {
    "editorial", "letter", "comment", "news", "biography",
    "case reports", "published erratum", "retraction of publication",
}


# ── Step 1: Category & ISSN resolution ───────────────────────────────────────

def resolve_categories(input_str: str) -> list[str]:
    if input_str.strip().lower() in ("all", ""):
        return ALL_CATEGORIES
    parts = [p.strip() for p in input_str.split(",")]
    matched = []
    for p in parts:
        for cat in ALL_CATEGORIES:
            if p.lower() in cat.lower() and cat not in matched:
                matched.append(cat)
    return matched if matched else ALL_CATEGORIES


def get_issns(selected_cats: list[str]) -> list[str]:
    issns = set()
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row_cats = [c.strip() for c in row["Categories"].split(";")]
            if any(c in row_cats for c in selected_cats):
                issn = (
                    row.get("ISSN (Online)", "").strip()
                    or row.get("ISSN (Print)", "").strip()
                )
                if issn:
                    issns.add(issn)
    # Sort for consistent ordering across chunks, then slice
    sorted_issns = sorted(issns)
    if CHUNK_TOTAL > 1:
        chunk_size = len(sorted_issns) // CHUNK_TOTAL
        start = (CHUNK_INDEX - 1) * chunk_size
        end = start + chunk_size if CHUNK_INDEX < CHUNK_TOTAL else len(sorted_issns)
        sorted_issns = sorted_issns[start:end]
        print(f"Chunk {CHUNK_INDEX}/{CHUNK_TOTAL}: ISSNs {start+1}–{end} of {len(issns)} total")
    return sorted_issns


# ── Step 2: PubMed search ─────────────────────────────────────────────────────

def pubmed_search(issns: list[str], start: str, end: str, topic: str) -> list[str]:
    """Return all PMIDs from journals in issns published between start and end."""
    all_pmids = []
    total_batches = (len(issns) + PUBMED_ISSN_BATCH - 1) // PUBMED_ISSN_BATCH
    for i in range(0, len(issns), PUBMED_ISSN_BATCH):
        batch = issns[i : i + PUBMED_ISSN_BATCH]
        issn_term = " OR ".join(f"{issn}[issn]" for issn in batch)
        term = f"({issn_term})"
        if topic:
            safe_topic = urllib.parse.quote(topic)
            term += f' AND ("{safe_topic}"[title/abstract])'
        url = (
            f"{PUBMED_BASE}/esearch.fcgi?db=pubmed"
            f"&term={urllib.parse.quote(term)}"
            f"&mindate={start}&maxdate={end}&datetype=pdat"
            f"&retmax=50&retmode=json"
        )
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            pmids = r.json().get("esearchresult", {}).get("idlist", [])
            all_pmids.extend(pmids)
            batch_num = i // PUBMED_ISSN_BATCH + 1
            print(f"  esearch batch {batch_num}/{total_batches}: {len(pmids)} PMIDs")
        except Exception as e:
            print(f"  esearch batch error: {e}")
        time.sleep(NCBI_DELAY)
    return list(set(all_pmids))


# ── Step 3: Fetch summaries & first-pass screen ───────────────────────────────

def fetch_summaries(pmids: list[str]) -> dict:
    summaries = {}
    for i in range(0, len(pmids), ESUMMARY_BATCH):
        batch = pmids[i : i + ESUMMARY_BATCH]
        url = f"{PUBMED_BASE}/esummary.fcgi?db=pubmed&id={','.join(batch)}&retmode=json"
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            result = r.json().get("result", {})
            for pmid in batch:
                article = result.get(pmid, {})
                if not article:
                    continue
                summaries[pmid] = {
                    "title":    article.get("title", "").strip(),
                    "journal":  article.get("fulljournalname", ""),
                    "pubdate":  article.get("pubdate", ""),
                    "doi":      next(
                        (x["value"] for x in article.get("articleids", []) if x["idtype"] == "doi"),
                        "",
                    ),
                    "pubtype":  [pt.lower() for pt in article.get("pubtype", [])],
                }
        except Exception as e:
            print(f"  esummary error: {e}")
        time.sleep(NCBI_DELAY)
    return summaries


def title_score(title: str) -> int:
    t = title.lower()
    return sum(1 for sig in GROUNDBREAKING_SIGNALS if sig in t)


def screen_candidates(summaries: dict) -> list[tuple]:
    """Score and filter summaries. Returns [(score, pmid, summary)] sorted by score desc."""
    scored = []
    for pmid, s in summaries.items():
        if SKIP_PUBTYPES & set(s["pubtype"]):
            continue
        if not s["title"]:
            continue
        score = title_score(s["title"])
        scored.append((score, pmid, s))
    scored.sort(key=lambda x: -x[0])
    return scored[:MAX_CANDIDATES]


# ── Step 4: SERPAPI Google News filter ───────────────────────────────────────

def news_hit_count(title: str) -> int:
    """Return number of Google News results for title. Returns -1 on error."""
    if not SERPAPI_KEY:
        return -1
    try:
        r = requests.get(
            SERPAPI_URL,
            params={"engine": "google_news", "q": title, "api_key": SERPAPI_KEY},
            timeout=15,
        )
        r.raise_for_status()
        return len(r.json().get("news_results", []))
    except Exception as e:
        print(f"  SERPAPI error for '{title[:60]}...': {e}")
        return -1


def apply_media_filter(candidates: list[tuple], threshold: int) -> list[tuple]:
    passed = []
    for score, pmid, s in candidates:
        count = news_hit_count(s["title"])
        if count == -1:
            print(f"  PMID {pmid}: SERPAPI error — including")
            passed.append((score, pmid, s))
        elif count < threshold:
            print(f"  PMID {pmid}: {count} news hits — ✓ included")
            passed.append((score, pmid, s))
        else:
            print(f"  PMID {pmid}: {count} news hits — skipped (widely covered)")
        time.sleep(SERPAPI_DELAY)
    return passed


# ── Step 5: Fetch abstracts ───────────────────────────────────────────────────

def fetch_abstract(pmid: str) -> str:
    url = (
        f"{PUBMED_BASE}/efetch.fcgi?db=pubmed"
        f"&id={pmid}&retmode=text&rettype=abstract"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return r.text.strip()
    except Exception as e:
        print(f"  efetch error for PMID {pmid}: {e}")
        return ""


# ── Step 6: Claude digest writer ─────────────────────────────────────────────

def write_digest(studies: list[dict], categories: list[str], start_label: str,
                 end_label: str, media_checked: bool) -> str:

    studies_block = ""
    for i, s in enumerate(studies, 1):
        studies_block += f"""
--- Study {i} ---
PMID: {s['pmid']}
Journal: {s['journal']}
Published: {s['pubdate']}
DOI: {s['doi'] or 'not available'}
Title: {s['title']}

Abstract:
{s['abstract']}
"""

    media_note = (
        "Not widely covered ✓ (verified via Google News / SERPAPI)"
        if media_checked
        else "Not verified — SERPAPI unavailable"
    )

    prompt = f"""You are writing a Mental Health & Brain Science Research Digest.

Coverage window: {start_label} to {end_label}
Categories: {', '.join(categories)}
{f'Topic focus: {TOPIC_FOCUS}' if TOPIC_FOCUS else ''}
Media screening: {media_note}

I'm giving you {len(studies)} studies that passed screening. For each one, write a digest entry using EXACTLY this format — no deviation:

### [N]. [Plain-language headline, present tense, no jargon]

**Journal:** *Journal name* | **Published:** Date
**PMID:** 12345678 | **DOI:** 10.xxxx/xxxx
**Groundbreaking because:** [one or more of: Counterintuitive finding / Overturns prior research / First-in-class human study / Included for relevance]
**Media coverage:** {media_note}

**The study:** 2–4 sentences. What researchers did, who participated (N=X, demographics), key finding in plain language.

**Why it matters:** 1–2 sentences. Real-world or clinical significance. Do NOT imply the reader should start, stop, or change any treatment.

**Caveats:** Comma-separated list of applicable flags: small sample (N<100), observational design, single-center, self-reported outcomes, short follow-up, industry funding [name funder], animal/in vitro study, preprint/not peer-reviewed, secondary analysis. Write "None identified" if none apply.

---

After all entries, append:

## Citation Reference

| # | PMID | Journal | Published | DOI |
|---|------|---------|-----------|-----|
[one row per study]

Writing rules:
- Never use: "breakthrough", "cure", "reverses", "eliminates", "proven to prevent"
- Always use: "suggests", "found that", "associated with", "early evidence indicates"
- If a study doesn't clearly fit any groundbreaking criterion, write "Included for relevance" and still write a full entry
- Be concise — each entry should be readable in under 2 minutes

Studies:
{studies_block}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── Step 7: Fact-check + New Scientist pitch ─────────────────────────────────

NS_STYLE_NOTES = """
New Scientist Mind section publishes 400–600 word news pieces. Style notes from recent examples:
- Headlines: present tense, counterintuitive framing, no jargon. E.g. "Vocal fry is more common in men, actually, find scientists" / "Epic dreaming is leaving people exhausted and distressed"
- Lead immediately with the surprising finding — no throat-clearing
- One quote from the lead researcher, one from an independent expert
- Acknowledge what the study can and can't tell us
- End with a practical implication or open question
- Audience: curious non-specialist adults interested in psychology, behaviour, and the mind
- Tone: warm, intelligent, slightly irreverent
- The section covers: relationships, sleep, emotion, cognition, mental health treatments, social behaviour, neurodiversity, perception
"""

def fact_check_and_pitch(draft_digest: str, studies: list[dict]) -> str:
    """
    Second Claude pass: fact-checks the draft against abstracts, improves caveats,
    and appends a New Scientist Mind pitch to each study entry.
    Returns an enriched version of the full digest.
    """
    studies_block = ""
    for i, s in enumerate(studies, 1):
        studies_block += f"""
--- Study {i} ---
PMID: {s['pmid']}
Title: {s['title']}
Abstract (source of truth):
{s['abstract']}
"""

    prompt = f"""You are a science editor reviewing a research digest draft. You have two jobs:

1. FACT-CHECK each entry against its abstract (the source of truth)
2. ADD a New Scientist Mind pitch to each entry

---

## DRAFT DIGEST TO REVIEW:

{draft_digest}

---

## ABSTRACTS (source of truth — one per study, matched by PMID):

{studies_block}

---

## YOUR TASKS:

### Task 1 — Fact-check each entry

For each study entry in the draft, verify against its abstract:
- Sample size (N) matches exactly
- Study design is correctly described (RCT, observational, cross-sectional, cohort, etc.)
- Direction of effect is correct (increased vs. decreased, positive vs. negative)
- Key statistics are accurately quoted or paraphrased
- No causal language used for observational findings
- Caveats section is complete — add any that were missed

If issues are found, correct them inline. Add a **Fact-check note** line after the Caveats line ONLY if you made a correction, using this format:
**Fact-check note:** [Brief plain-language description of what was corrected]

If everything checks out, do not add a fact-check note.

### Task 2 — Add a New Scientist Mind pitch to each entry

After the Caveats line (and any fact-check note), add:

**New Scientist Mind pitch:**
- **Suggested headline:** [Present tense, punchy, counterintuitive if possible — modelled on NS Mind style]
- **Hook:** [One sentence that would open the article — lead with the surprising or counterintuitive finding]
- **Why it fits Mind:** [One sentence on why NS Mind readers — curious non-specialists interested in psychology, behaviour, social science — would care]
- **Pitch angle:** [2–3 sentences. What happened, what's surprising about it, what it means. Written as if pitching to a commissioning editor. Note any independent expert angle or societal hook.]
- **Caveats to flag in article:** [Any limitations the journalist should acknowledge — can repeat from Caveats above or add nuance]

---

{NS_STYLE_NOTES}

---

## OUTPUT FORMAT

Return the COMPLETE digest with all entries, incorporating your fact-check corrections and pitch additions. Keep the original structure and citation table intact. Do not summarise or truncate — output the full text."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=12000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── Step 8: Email via Resend ──────────────────────────────────────────────────

def md_to_html(md: str) -> str:
    """Convert markdown digest to clean HTML for email."""
    html = md

    # Escape existing HTML
    html = html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Headers
    html = re.sub(r"^### (.+)$", r"<h3 style='margin-top:2em;color:#1a1a2e;'>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",  r"<h2 style='margin-top:2em;border-bottom:1px solid #ddd;padding-bottom:4px;'>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",   r"<h1 style='color:#1a1a2e;'>\1</h1>", html, flags=re.MULTILINE)

    # New Scientist pitch block — wrap in a styled callout box
    html = re.sub(
        r"\*\*New Scientist Mind pitch:\*\*\n(.*?)(?=\n---|\n### |\Z)",
        lambda m: (
            "<div style='background:#f0f7ff;border-left:4px solid #2563eb;padding:14px 18px;"
            "margin:1.2em 0;border-radius:0 6px 6px 0;font-size:0.95em;'>"
            "<strong style='color:#1d4ed8;'>📰 New Scientist Mind pitch</strong><br><br>"
            + m.group(1).replace("\n", "<br>")
            + "</div>"
        ),
        html,
        flags=re.DOTALL,
    )

    # Fact-check note — style as a warning
    html = re.sub(
        r"\*\*Fact-check note:\*\* (.+)",
        r"<div style='background:#fff8e1;border-left:4px solid #f59e0b;padding:8px 14px;"
        r"margin:0.6em 0;border-radius:0 4px 4px 0;font-size:0.9em;'>"
        r"⚠️ <strong>Fact-check note:</strong> \1</div>",
        html,
    )

    # Bold + italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",     r"<em>\1</em>", html)

    # Horizontal rules
    html = re.sub(r"^---$", "<hr style='border:none;border-top:1px solid #eee;margin:1.5em 0;'>", html, flags=re.MULTILINE)

    # Markdown table → HTML table
    def table_to_html(match):
        lines = [l.strip() for l in match.group(0).strip().split("\n") if l.strip()]
        rows = [l.split("|")[1:-1] for l in lines if not re.match(r"^\|[\s\-|]+\|$", l)]
        if not rows:
            return match.group(0)
        thead = "".join(f"<th style='padding:6px 10px;text-align:left;background:#f0f0f0;'>{c.strip()}</th>" for c in rows[0])
        tbody = ""
        for row in rows[1:]:
            tbody += "<tr>" + "".join(f"<td style='padding:6px 10px;border-top:1px solid #eee;'>{c.strip()}</td>" for c in row) + "</tr>"
        return f"<table style='border-collapse:collapse;width:100%;font-size:0.9em;'><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"

    html = re.sub(r"(\|.+\|\n)+", table_to_html, html)

    # Paragraphs (double newline)
    paragraphs = re.split(r"\n{2,}", html)
    processed = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if p.startswith("<h") or p.startswith("<hr") or p.startswith("<table"):
            processed.append(p)
        else:
            p = p.replace("\n", "<br>")
            processed.append(f"<p style='margin:0.8em 0;line-height:1.7;'>{p}</p>")
    html = "\n".join(processed)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;max-width:680px;margin:auto;padding:24px;color:#222;line-height:1.6;font-size:16px;">
{html}
<hr style="margin-top:3em;border:none;border-top:1px solid #eee;">
<p style="font-size:0.8em;color:#888;">Mental Health &amp; Brain Science Research Digest · Powered by PubMed + Claude + SERPAPI</p>
</body>
</html>"""


def send_email(subject: str, html: str):
    r = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {RESEND_KEY}",
            "Content-Type": "application/json",
        },
        json={"from": FROM_EMAIL, "to": [RECIPIENT], "subject": subject, "html": html},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)
    start_str   = start.strftime("%Y/%m/%d")
    end_str     = now.strftime("%Y/%m/%d")
    start_label = start.strftime("%b %d, %Y")
    end_label   = now.strftime("%b %d, %Y")

    print("=" * 60)
    print("Mental Health & Brain Science Research Digest")
    print(f"Coverage: {start_label} → {end_label}")
    print("=" * 60)

    # 1. Categories & ISSNs
    categories = resolve_categories(CATEGORIES_INPUT)
    print(f"\nCategories ({len(categories)}): {', '.join(categories)}")
    issns = get_issns(categories)
    print(f"ISSNs: {len(issns)}")

    # 2. PubMed search
    print(f"\nSearching PubMed across {len(issns)} journals...")
    pmids = pubmed_search(issns, start_str, end_str, TOPIC_FOCUS)
    print(f"Total PMIDs: {len(pmids)}")

    # 3. Summaries & title screening
    print("\nFetching summaries...")
    summaries = fetch_summaries(pmids)
    candidates = screen_candidates(summaries)
    print(f"Candidates after title screen: {len(candidates)}")

    # 4. SERPAPI media filter
    media_checked = bool(SERPAPI_KEY)
    if media_checked:
        print(f"\nRunning SERPAPI news filter (threshold: <{MEDIA_THRESHOLD} hits)...")
        passed = apply_media_filter(candidates, MEDIA_THRESHOLD)
        if len(passed) < MIN_STUDIES:
            print(f"Only {len(passed)} passed — relaxing threshold to <{MEDIA_RELAX}...")
            passed = apply_media_filter(candidates, MEDIA_RELAX)
    else:
        print("\nSERPAPI key not set — skipping media filter")
        passed = candidates

    print(f"Studies after media filter: {len(passed)}")

    # 5. Fetch abstracts
    print("\nFetching abstracts...")
    studies = []
    for _, pmid, s in passed:
        abstract = fetch_abstract(pmid)
        time.sleep(NCBI_DELAY)
        if not abstract:
            print(f"  PMID {pmid}: no abstract — skipped")
            continue
        studies.append({
            "pmid":     pmid,
            "title":    s["title"],
            "journal":  s["journal"],
            "pubdate":  s["pubdate"],
            "doi":      s.get("doi", ""),
            "abstract": abstract,
        })
        print(f"  PMID {pmid}: abstract fetched ✓")

    print(f"\nStudies with abstracts: {len(studies)}")

    if not studies:
        print("No studies to include — exiting without sending email.")
        return

    # 6. Write digest with Claude (pass 1 — writer)
    print("\nWriting digest with Claude (pass 1 — writer)...")
    draft_digest = write_digest(studies, categories, start_label, end_label, media_checked)

    # 7. Fact-check + New Scientist pitch (pass 2 — editor)
    print("Fact-checking and adding New Scientist pitches (pass 2 — editor)...")
    digest_body = fact_check_and_pitch(draft_digest, studies)

    # 9. Assemble full digest
    cats_label = (
        ", ".join(categories) if len(categories) <= 3
        else f"{', '.join(categories[:3])} + {len(categories) - 3} more"
    )
    chunk_label = f" ({CHUNK_INDEX}/{CHUNK_TOTAL})" if CHUNK_TOTAL > 1 else ""
    header = f"""# Mental Health & Brain Science Research Digest
**Run date:** {now.strftime('%B %d, %Y')} | **Coverage:** {start_label} – {end_label}
**Categories:** {cats_label}{chunk_label} | **Journals searched:** {len(issns)}
{f'**Focus:** {TOPIC_FOCUS}' if TOPIC_FOCUS else ''}
**Studies surfaced:** {len(studies)} | **Media-screened:** {'Yes — SERPAPI Google News' if media_checked else 'No — SERPAPI key not configured'}

---

"""
    full_md = header + digest_body

    # 10. Send email
    unverified_flag = " [unverified media filter]" if not media_checked else ""
    subject = (
        f"Mental Health Digest — {cats_label}{chunk_label}"
        f" | {now.strftime('%b %d, %Y')} | {len(studies)} Studies{unverified_flag}"
    )
    html = md_to_html(full_md)

    print(f"\nSending email to {RECIPIENT}...")
    result = send_email(subject, html)
    print(f"Email sent ✓  id={result.get('id', 'unknown')}")
    print("\nDone.")


if __name__ == "__main__":
    main()
