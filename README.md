# Women's Health Research Digest

A GitHub Actions workflow that searches curated women's health, reproductive medicine, hormones and endocrinology, women's mental health, and nutrition journals on PubMed, filters out widely covered stories, runs a single Claude pass for journalist-ready summaries and pitch angles, and publishes results to a GitHub Pages dashboard.

## How it works

1. **PubMed search** - Queries journals by ISSN for studies published in the past 7 days
2. **Title screening** - Prioritizes studies with novelty signals and excludes animal-only studies
3. **SERPAPI media filter** - Checks Google News and skips any study with 3+ news results
4. **Abstract fetch** - Retrieves full abstracts for shortlisted studies
5. **Claude pass** - Writes structured JSON: headline, summary, why it matters, caveats, relevance score, and pitch angles per publication type
6. **Artifact upload** - Saves JSON results as a GitHub Actions artifact
7. **Deploy job** - Downloads all job artifacts, merges and deduplicates by PMID, commits `data/results.json`, serves via GitHub Pages
8. **Email notification** - Sends a short email with study count and a dashboard link

## Dashboard

Features:
- Card view per study with headline, summary, caveats, fact-check notes
- Expandable pitch angles section for publications such as Women's Health Magazine, Prevention, Self, Well+Good, The Cut, Shape, and general health outlets
- Filter by category, groundbreaking type, status, date range, and score
- Search across all study text and pitches
- Status tracking (New / Saved / Pitched / Passed) saved to localStorage
- Deduplication across runs by PMID

## Schedule

Runs automatically every morning at 7:00 AM ET. All jobs run in parallel; the deploy job merges results and publishes the dashboard once complete.

Can also be triggered manually via **Actions -> Women's Health Research Digest -> Run workflow**.

## Categories

| Category | Journals | Jobs |
|---|---:|---|
| Womens Health & Reproduction | 144 | 2 (chunks 1-2) |
| Endocrinology & Metabolism | 152 | 2 (chunks 1-2) |
| Psychiatry | 216 | 2 (chunks 1-2) |
| Nutritional Sciences | 62 | 2 (chunks 1-2) |

Large categories are split into chunks to keep run times under 20 minutes.

## Manual Trigger

Go to **Actions -> Women's Health Research Digest -> Run workflow**.

- Leave **category** blank to run all jobs
- Enter an exact category name, such as `Womens Health & Reproduction`, to run just that category

## GitHub Pages Setup

1. Go to **Settings -> Pages**
2. Set source to **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)`
4. Save; GitHub will serve `index.html` at the dashboard URL

## Required Secrets

Add these in **Settings -> Secrets and variables -> Actions**:

| Secret | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `SERPAPI_KEY` | SerpAPI key for Google News filtering |
| `RESEND_API_KEY` | Resend API key for email delivery |
| `DASHBOARD_URL` | Full URL of your GitHub Pages dashboard |

## Repo Structure

```text
.github/
  workflows/
    womens-health-digest.yml
scripts/
  womens_health_digest.py
  merge_results.py
  extract_journals.py
data/
  Womens Health & Reproduction.csv
  Endocrinology & Metabolism.csv
  Psychiatry.csv
  Nutritional Sciences.csv
  results.json
index.html
requirements.txt
```

## Dashboard Study Card Fields

Each study card shows:

- **Headline** - plain-language present-tense summary
- **Relevance score** - 1-10, weighted for women's health, wellness, and medicine journalism fit
- **Category & journal** - source metadata
- **Groundbreaking type** - counterintuitive, overturns prior research, first-in-class, or domain-relevant finding
- **Media coverage** - SERPAPI verification status
- **The study** - what was done, who participated, and the key finding
- **Why it matters** - real-world significance for the target audience
- **Caveats** - limitations flagged automatically
- **Fact-check note** - corrections made during the Claude pass
- **Pitch angles** - expandable publication-specific pitch blocks
- **Status** - New / Saved / Pitched / Passed, tracked in your browser
