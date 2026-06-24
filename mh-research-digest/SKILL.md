---
name: mh-research-digest
description: Search a curated list of mental health and brain science journals for groundbreaking studies published in the past 45 days that haven't been widely covered in the media. Presents a subcategory menu, screens candidates via SERPAPI Google News, and emails a formatted digest. Triggers: "run the mental health digest", "search mental health journals", "brain science digest", "recent psych research", "mh digest".
---

# Mental Health & Brain Science Research Digest

## Overview

This skill searches a curated CSV of mental health and brain science journals for recent, groundbreaking studies — ones that are counterintuitive, overturn prior research, or represent a first-in-class finding — and that have not been widely covered in the news. It emails the final digest to meagan.lea.morris@gmail.com.

**Coverage window:** 30 days back from today  
**Groundbreaking criteria:** Counterintuitive findings, overturns prior research, first-in-class human studies  
**Media filter:** SERPAPI Google News check — skip studies with 3+ news results  
**Output:** Email to meagan.lea.morris@gmail.com

---

## Step 1 — Interactive Setup

Ask the user two short questions before proceeding.

### 1a. Subcategory selection

Present this menu:

```
Which journal category (or categories) should I search? Pick by number or name — you can choose more than one.

 1. Psychology (135 journals)
 2. Psychiatry (111 journals)
 3. Behavioral Sciences (88 journals)
 4. Brain (23 journals)
 5. Psychophysiology (22 journals)
 6. Neurology (21 journals)
 7. Psychopharmacology (14 journals)
 8. Social Sciences (7 journals)
 9. Substance-Related Disorders (4 journals)
10. All of the above
```

If the user selects 10 or says "all", include every category above.

### 1b. Topic focus (optional)

Ask: *"Is there a specific topic or theme you want to focus on — or should I search broadly across the selected categories?"*

Examples: "adolescent depression", "sleep and cognition", "psychedelic therapy", "trauma and memory"

If none specified, search broadly.

---

## Step 2 — Parse the CSV and Collect ISSNs

The CSV is embedded in this skill package at `Mental Health - Brain Mental Health.csv`.

Use bash with Python to parse it:

```python
import csv

csv_path = "/path/to/Mental Health - Brain Mental Health.csv"  # adjust to actual skill path

selected_categories = ["Psychology", "Psychiatry"]  # adjust to user selection

issns = []
with open(csv_path, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        row_cats = [c.strip() for c in row['Categories'].split(';')]
        if any(cat in row_cats for cat in selected_categories):
            issn = row.get('ISSN (Online)', '').strip() or row.get('ISSN (Print)', '').strip()
            if issn:
                issns.append(issn)

# Deduplicate
issns = list(set(issns))
print(f"Found {len(issns)} ISSNs")
print(issns)
```

Collect all unique ISSNs. Prefer ISSN (Online); fall back to ISSN (Print) if Online is blank. Skip rows where both are empty.

---

## Step 3 — Search PubMed

All HTTP calls via `mcp__workspace__web_fetch`. Do NOT use bash for HTTP requests.

### Date range

30 days back from today. Calculate start date as `today - 30 days`. Format: `YYYY/MM/DD`.

### esearch URL pattern

```
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=ISSN1[issn]+OR+ISSN2[issn]+OR+ISSN3[issn]&mindate=STARTDATE&maxdate=ENDDATE&datetype=pdat&retmax=50&retmode=json
```

**Critical:** Maximum 3 ISSNs per URL. Batch all ISSNs into groups of 3 and run a separate esearch call per batch. Collect all returned PMIDs.

If a topic focus was specified, add it to each batch's search term:
```
&term=ISSN1[issn]+OR+ISSN2[issn]+AND+("topic focus"[title/abstract])
```

---

## Step 4 — Fetch Summaries and Screen Candidates

Fetch article summaries for all collected PMIDs in batches:

```
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id=PMID1,PMID2,...&retmode=json
```

Use batches of up to 20 PMIDs per call.

### First-pass screening — title-level

From the title and source journal alone, **prioritize** articles that signal:
- Surprising or counterintuitive results ("unexpected", "contrary to", "paradoxical", "against prior", "challenges the assumption")
- First-in-class language ("first study", "novel", "previously unknown", "newly identified")
- Overturning language ("replication failure", "no evidence for", "reverses", "contradicts", "debunks")
- Strong clinical novelty (new treatment mechanism, new biomarker, new population studied)

**Skip at this stage:**
- Editorials, letters, commentaries, reviews, meta-analyses (unless the meta-analysis overturns prior consensus)
- Animal-only or in vitro studies (unless mechanism is truly exceptional)
- Pure methodology / measurement papers
- Case reports (N=1 or N<5)

Aim to carry forward 15–25 candidate PMIDs for the next steps.

---

## Step 5 — SERPAPI Google News Check

For each candidate PMID, fetch the article title from the summary data, then run a SERPAPI Google News search to check media coverage.

**SERPAPI call pattern:**

```
https://serpapi.com/search.json?engine=google_news&q=[URL-ENCODED ARTICLE TITLE]&api_key=[SERPAPI_KEY]
```

- URL-encode the title (replace spaces with `+`, encode special characters)
- Use `mcp__workspace__web_fetch` for this call

**Filtering rule:** If the response returns 3 or more news articles in the `news_results` array, the study has been widely covered — **skip it**.

If SERPAPI is not configured (no API key available), note this to the user and proceed without the news check, flagging the omission in the email.

**Important:** After the SERPAPI filter, you should have a refined list of candidates. If fewer than 5 remain, relax the media filter threshold to 5+ news results and recheck the dropped candidates. If still fewer than 5, proceed with what you have and note in the email that the selection pool was limited.

---

## Step 6 — Fetch Abstracts

Fetch the full abstract for each remaining candidate via `mcp__workspace__web_fetch`:

```
https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=PMID1,PMID2,PMID3&retmode=text&rettype=abstract
```

**Critical:** Maximum 3–4 PMIDs per call. Always use `retmode=text&rettype=abstract`. Drop any article that returns no abstract.

---

## Step 7 — Score for Groundbreaking Criteria

For each abstract, evaluate against these three criteria. A study must meet **at least one** to be included:

### Criterion A — Counterintuitive / Unexpected
The finding goes against what a well-informed clinician or researcher would expect. Ask: *"Would a specialist in this field be surprised by this result?"*

Signal phrases: unexpected association, no significant effect (when one was predicted), opposite direction, protective effect of something assumed harmful, harmful effect of something assumed neutral.

### Criterion B — Overturns Prior Research
The study explicitly contradicts, fails to replicate, or significantly updates a previously established finding.

Signal phrases: contrary to previous studies, failed replication, prior estimates were inflated/underestimated, revised understanding, challenges the current model.

### Criterion C — First-in-Class
This is the first human study of a mechanism, treatment approach, intervention, or population. Or a novel biomarker / imaging finding with strong translational potential.

Signal phrases: first randomized trial of, first longitudinal study, first study to examine, newly identified, previously undescribed, no prior human data.

Studies meeting 2 or 3 criteria should be ranked higher in the digest.

---

## Step 8 — Write the Digest

Surface all studies that passed the filter and met at least one groundbreaking criterion. No cap on count, but aim for 5–12 entries.

### Entry format

```markdown
### [Number]. [Headline — plain language, present tense, no jargon]

**Journal:** *Name* | **Published:** Date
**PMID:** [ID] | **DOI:** [DOI if available]
**Groundbreaking because:** [One of: Counterintuitive finding / Overturns prior research / First-in-class / 2–3 criteria]
**Media coverage:** Not widely covered ✓ [or: SERPAPI check skipped — not verified]

**The study:** What researchers did, who the participants were (N=, demographic details), key finding in 2–4 plain-language sentences.

**Why it matters:** Clinical or real-world significance in 1–2 sentences. Do not imply clinical action from preliminary or observational data.

**Caveats:** [Flag applicable items from Standing Rules below]
```

### Digest header

```
# Mental Health & Brain Science Research Digest
**Run date:** [Date] | **Coverage window:** [Start] – [End]
**Categories searched:** [Names] | **Journals searched:** [N ISSNs]
[If topic focus: **Focus:** [Topic]]
**Studies surfaced:** [N] | **Media-screened:** [Y/N]
```

### Citation table

```markdown
## Citation Reference

| # | PMID | Journal | Published | DOI |
|---|------|---------|-----------|-----|
```

---

## Step 9 — Send Email

Send the digest by email to **meagan.lea.morris@gmail.com** using the Gmail MCP tool (`mcp__33d9aea4-1651-4ebb-914e-c3bccc53c5c5__create_draft` or the send equivalent).

**Subject line:** `Mental Health Research Digest — [Month DD, YYYY] | [N] Studies`

**Body:** Full digest in plain text or HTML (prefer HTML if supported for readability). Include the citation table at the bottom.

If email sending fails, save the digest as a `.md` file in the outputs folder and tell the user where to find it.

---

## Standing Rules

### Study selection priorities
1. Human subjects, prospective or RCT designs — highest priority
2. Large cohorts (N ≥ 100) — prioritize
3. Observational / cross-sectional — acceptable if finding is highly novel
4. Animal / in vitro — only if mechanism is exceptional and clearly translational; flag in Caveats

### Caveats — flag any that apply
- Small sample (N < 100)
- Single-center study
- Observational design ("cannot establish causation")
- Self-reported outcomes
- Short follow-up period
- Industry funding or author conflict of interest (name the funder)
- Population may not generalize (note specifics)
- Lack of control group
- Preprint / not yet peer-reviewed
- Secondary analysis or reanalysis

### Framing rules
- Never use "breakthrough", "cure", "reverses", "eliminates", or "proven to prevent"
- Use: "suggests", "found that", "associated with", "early evidence indicates"
- Do not imply a reader should start, stop, or change treatment based on a single study

### Deduplication
Do not include the same study in two consecutive digest runs within the same session.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| esearch returns 0 results | Check ISSN format — must be bare number like `1552-5260`, no extra characters |
| URL fetch fails | Reduce to 2 ISSNs per call |
| Abstract is empty | Editorial/commentary only — drop it |
| efetch response too long | Reduce to 2 PMIDs per call |
| Bash HTTP call fails | All PubMed and SERPAPI calls must use `mcp__workspace__web_fetch` — bash HTTP is blocked |
| SERPAPI returns error / no key | Skip news check, note omission in email subject: `[unverified media filter]` |
| Fewer than 5 studies pass all filters | Relax media threshold to 5+ news results; if still <5, include what remains and note it |
| Gmail send fails | Save digest as `.md` in outputs folder and report path to user |
