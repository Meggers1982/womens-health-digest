#!/usr/bin/env python3
"""
Women's Health Research Digest
Searches PubMed, filters via SERPAPI, writes structured JSON with Claude, sends short email via Resend.
Results are saved as a GitHub Actions artifact and merged into data/results.json by the deploy job,
which powers the GitHub Pages dashboard.

Required environment variables:
  ANTHROPIC_API_KEY   — Anthropic API key
  RESEND_API_KEY      — Resend API key

Optional environment variables:
  SERPAPI_KEY         — SerpAPI key (skips news filter if not set)
  RECIPIENT_EMAIL     — Override recipient (default: meagan.lea.morris@gmail.com)
  FROM_EMAIL          — Verified Resend sender (default: onboarding@resend.dev)
  CATEGORIES          — Comma-separated category names or "all" (default: all)
  TOPIC_FOCUS         — Optional topic to narrow the search
  CHUNK_INDEX         — 1-based chunk index (default: 1)
  CHUNK_TOTAL         — Total chunks for this category (default: 1)
  FILTER_CATEGORY     — If set, jobs whose CATEGORIES don't match will exit early
  DASHBOARD_URL       — URL of the GitHub Pages dashboard for the notification email
  SUPABASE_URL        — Supabase project URL (enables personalization from dashboard save/delete feedback)
  SUPABASE_KEY        — Supabase API key (read-only use; skips personalization if not set)
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

ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SERPAPI_KEY      = os.environ.get("SERPAPI_KEY", "")
RESEND_KEY       = os.environ["RESEND_API_KEY"]
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
RECIPIENT        = os.environ.get("RECIPIENT_EMAIL", "meagan.lea.morris@gmail.com")
CATEGORIES_INPUT = os.environ.get("CATEGORIES", "all")
TOPIC_FOCUS      = os.environ.get("TOPIC_FOCUS", "").strip()
CHUNK_INDEX      = int(os.environ.get("CHUNK_INDEX", "1"))
CHUNK_TOTAL      = int(os.environ.get("CHUNK_TOTAL", "1"))
FILTER_CATEGORY  = os.environ.get("FILTER_CATEGORY", "").strip()
DASHBOARD_URL    = os.environ.get("DASHBOARD_URL", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
SOURCE_ID        = "womens-health"
RESULTS_PATH     = Path("/tmp/results.json")

DATA_DIR = Path(__file__).parent.parent / "data"

ALL_CATEGORIES = [
    "Womens Health & Reproduction",
    "Endocrinology & Metabolism",
    "Psychiatry",
    "Nutritional Sciences",
]

DAYS_BACK         = 7
MEDIA_THRESHOLD   = 3
MEDIA_RELAX       = 5
MIN_STUDIES       = 5
MAX_CANDIDATES    = 30
MIN_TITLE_SCORE   = 1
MIN_RELEVANCE_SCORE = 4  # drop studies scored 3 or below — too weak/niche to pitch
ABSTRACT_MAX_CHARS = 5000
PUBMED_ISSN_BATCH = 3
ESUMMARY_BATCH    = 20
NCBI_DELAY        = 0.4
SERPAPI_DELAY     = 1.0
CLAUDE_BATCH_SIZE = 5
CLAUDE_MODEL      = "claude-sonnet-4-6"

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SERPAPI_URL = "https://serpapi.com/search.json"
RESEND_URL  = "https://api.resend.com/emails"

GROUNDBREAKING_SIGNALS = [
    "first", "novel", "unexpected", "contrary", "paradox", "no evidence",
    "challenges", "reverses", "debunks", "replication failure", "previously unknown",
    "newly identified", "overturns", "failed to replicate", "against prior",
    "first randomized", "first longitudinal", "first human", "first study",
    "surpris", "counterintuitive", "revised understanding", "new mechanism",
    "no significant", "opposite", "protective effect",
]

SKIP_PUBTYPES = {
    "editorial", "letter", "comment", "news", "biography",
    "case reports", "published erratum", "retraction of publication",
}

ANIMAL_ONLY_SIGNALS = {
    "in mice", "in rats", "in mouse", "in rat", "mouse model", "rat model",
    "murine", "rodent model", "in zebrafish", "in drosophila", "in c. elegans",
    "in vivo model", "animal model", "in vitro", "cell line", "in silico",
    "in monkeys", "in primates", "primate model", "in pigs", "in rabbits",
}

HUMAN_SIGNALS = {
    "patient", "patients", "human", "humans", "adult", "adults", "children",
    "cohort", "clinical trial", "randomized", "participants", "men", "women",
    "adolescent", "population", "longitudinal", "cross-sectional", "survey",
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


def is_animal_only(title: str) -> bool:
    t = title.lower()
    return any(sig in t for sig in ANIMAL_ONLY_SIGNALS) and not any(sig in t for sig in HUMAN_SIGNALS)


def get_issns(selected_cats: list[str]) -> list[str]:
    issns = set()
    for cat in selected_cats:
        csv_path = DATA_DIR / f"{cat}.csv"
        if not csv_path.exists():
            print(f"  WARNING: {csv_path} not found — skipping")
            continue
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row_cats = [c.strip() for c in row["Categories"].split(";")]
                if any(c in row_cats or cat in c for c in row_cats) or cat in selected_cats:
                    issn = row.get("ISSN (Online)", "").strip() or row.get("ISSN (Print)", "").strip()
                    if issn:
                        issns.add(issn)
    sorted_issns = sorted(issns)
    if CHUNK_TOTAL > 1:
        chunk_size = len(sorted_issns) // CHUNK_TOTAL
        start = (CHUNK_INDEX - 1) * chunk_size
        end = start + chunk_size if CHUNK_INDEX < CHUNK_TOTAL else len(sorted_issns)
        print(f"Chunk {CHUNK_INDEX}/{CHUNK_TOTAL}: ISSNs {start+1}–{end} of {len(issns)} total")
        sorted_issns = sorted_issns[start:end]
    return sorted_issns


# ── Step 2: PubMed search ────────────────────────────────────────────────────

def pubmed_search(issns: list[str], start: str, end: str, topic: str) -> list[str]:
    all_pmids = []
    total_batches = (len(issns) + PUBMED_ISSN_BATCH - 1) // PUBMED_ISSN_BATCH
    for i in range(0, len(issns), PUBMED_ISSN_BATCH):
        batch = issns[i : i + PUBMED_ISSN_BATCH]
        issn_term = " OR ".join(f"{issn}[issn]" for issn in batch)
        term = f"({issn_term})"
        if topic:
            term += f' AND ("{urllib.parse.quote(topic)}"[title/abstract])'
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
            print(f"  esearch batch {i//PUBMED_ISSN_BATCH+1}/{total_batches}: {len(pmids)} PMIDs")
        except Exception as e:
            print(f"  esearch error: {e}")
        time.sleep(NCBI_DELAY)
    return list(set(all_pmids))


# ── Step 3: Summaries & screening ────────────────────────────────────────────

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
                    "title":   article.get("title", "").strip(),
                    "journal": article.get("fulljournalname", ""),
                    "pubdate": article.get("pubdate", ""),
                    "doi":     next((x["value"] for x in article.get("articleids", []) if x["idtype"] == "doi"), ""),
                    "pubtype": [pt.lower() for pt in article.get("pubtype", [])],
                }
        except Exception as e:
            print(f"  esummary error: {e}")
        time.sleep(NCBI_DELAY)
    return summaries


def title_score(title: str) -> int:
    t = title.lower()
    return sum(1 for sig in GROUNDBREAKING_SIGNALS if sig in t)


def screen_candidates(summaries: dict) -> list[tuple]:
    scored = []
    zero_score = []
    for pmid, s in summaries.items():
        if SKIP_PUBTYPES & set(s["pubtype"]):
            continue
        if not s["title"]:
            continue
        if is_animal_only(s["title"]):
            print(f"  PMID {pmid}: animal-only title — skipped")
            continue
        score = title_score(s["title"])
        if score >= MIN_TITLE_SCORE:
            scored.append((score, pmid, s))
        else:
            zero_score.append((score, pmid, s))
    scored.sort(key=lambda x: -x[0])
    result = scored[:MAX_CANDIDATES]
    if len(result) < MIN_STUDIES:
        slots = MIN_STUDIES - len(result)
        result.extend(zero_score[:slots])
        print(f"  Added {min(slots, len(zero_score))} zero-score study/studies to reach MIN_STUDIES")
    print(f"  {len(scored)} novelty-scored, {len(zero_score)} zero-score (skipped unless below MIN_STUDIES)")
    return result


# ── Step 4: SERPAPI filter ───────────────────────────────────────────────────

def news_hit_count(title: str) -> int:
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
        print(f"  SERPAPI error: {e}")
        return -1


def apply_media_filter(candidates: list[tuple], threshold: int) -> list[tuple]:
    passed = []
    for score, pmid, s in candidates:
        count = news_hit_count(s["title"])
        if count == -1:
            passed.append((score, pmid, s))
        elif count < threshold:
            print(f"  PMID {pmid}: {count} hits — ✓")
            passed.append((score, pmid, s))
        else:
            print(f"  PMID {pmid}: {count} hits — skipped")
        time.sleep(SERPAPI_DELAY)
    return passed


# ── Step 5: Fetch abstracts ──────────────────────────────────────────────────

def fetch_doi_content(doi: str) -> str:
    if not doi:
        return ""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; research-digest-bot/1.0; "
                "+https://github.com/Meggers1982/womens-health-digest)"
            ),
            "Accept": "text/html,application/xhtml+xml",
        }
        r = requests.get(f"https://doi.org/{doi}", headers=headers, timeout=20, allow_redirects=True)
        r.raise_for_status()
        html = r.text

        for pattern in [
            r'<meta[^>]+name="citation_abstract"[^>]+content="([^"]{200,})"',
            r'<meta[^>]+content="([^"]{200,})"[^>]+name="citation_abstract"',
        ]:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        for pattern in [
            r'<(?:div|section)[^>]+id="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section)>',
            r'<(?:div|section)[^>]+class="[^"]*abstract[^"]*"[^>]*>(.*?)</(?:div|section)>',
            r'<p[^>]+class="[^"]*abstract[^"]*"[^>]*>(.*?)</p>',
        ]:
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if m:
                text = re.sub(r'<[^>]+>', ' ', m.group(1)).strip()
                text = re.sub(r'\s+', ' ', text)
                if len(text) > 200:
                    return text

        return ""
    except Exception as e:
        print(f"  DOI fetch error ({doi}): {e}")
        return ""


def fetch_abstract(pmid: str, doi: str = "") -> str:
    pubmed_text = ""
    url = f"{PUBMED_BASE}/efetch.fcgi?db=pubmed&id={pmid}&retmode=text&rettype=abstract"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        pubmed_text = r.text.strip()
        if len(pubmed_text) > ABSTRACT_MAX_CHARS:
            pubmed_text = pubmed_text[:ABSTRACT_MAX_CHARS] + "… [truncated]"
    except Exception as e:
        print(f"  efetch error for {pmid}: {e}")

    if doi:
        time.sleep(NCBI_DELAY)
        doi_text = fetch_doi_content(doi)
        if doi_text and len(doi_text) > len(pubmed_text):
            print(f"  {pmid}: DOI abstract used ({len(doi_text)} chars vs PubMed {len(pubmed_text)} chars)")
            return doi_text

    return pubmed_text


# ── Step 5b: Feedback-based personalization ──────────────────────────────────

def build_personalization(source_id: str, results_path: Path) -> str:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/study_status",
            params={"study_id": f"like.{source_id}:*", "select": "study_id,status,updated_at"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  Supabase feedback fetch error: {e}")
        return ""

    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    def pmids(statuses):
        return {
            row["study_id"].split(":", 1)[1]
            for row in rows
            if row["status"] in statuses and row["updated_at"] >= cutoff
        }

    deleted, saved = pmids({"deleted", "passed"}), pmids({"saved", "pitched"})
    if not deleted and not saved:
        return ""

    history = {}
    if results_path.exists():
        try:
            history = {s["pmid"]: s for s in json.loads(results_path.read_text()).get("studies", [])}
        except Exception:
            pass

    def headlines(pmid_set, limit=8):
        return [history[p]["headline"][:80] for p in pmid_set if p in history and history[p].get("headline")][:limit]

    saved_ex, deleted_ex = headlines(saved), headlines(deleted)
    if not saved_ex and not deleted_ex:
        return ""

    block = (
        "Personalization based on this journalist's past feedback on this digest "
        "(soft signal only — nudge relevance_score by at most ±1, never exclude solely because of this):\n"
    )
    if saved_ex:
        block += "Recently SAVED (favor similar angles when the science supports it):\n" + "\n".join(f"  - {h}" for h in saved_ex) + "\n"
    if deleted_ex:
        block += "Recently PASSED ON / DELETED (be more conservative with similar angles):\n" + "\n".join(f"  - {h}" for h in deleted_ex) + "\n"
    print(f"  Personalization: {len(saved_ex)} saved example(s), {len(deleted_ex)} deleted example(s)")
    return block


# ── Step 6: Claude — single combined pass ────────────────────────────────────

def extract_json(text: str) -> list:
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"(\[.*\])", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return json.loads(text)


def process_batch(batch: list[dict], start_num: int, media_checked: bool, personalization: str = "") -> list[dict]:
    media_note = "Not widely covered ✓ (SERPAPI verified)" if media_checked else "Not verified — SERPAPI unavailable"

    studies_block = ""
    for i, s in enumerate(batch):
        studies_block += f"""
---
Study {start_num + i} | PMID: {s['pmid']} | {s['journal']} | {s['pubdate']}
Title: {s['title']}
Abstract: {s['abstract']}
"""

    prompt = f"""You are a science writer and editor for a Women's Health Research Digest, writing for a journalist audience covering women's health, reproductive medicine, hormonal health, women's mental health, and nutrition.

Your readers pitch to publications like Women's Health Magazine, Prevention, Self, Well+Good, The Cut (health section), Shape, SELF, and similar women-focused or general health outlets.

For each study below, return a single JSON array. Each object must have exactly these keys:

{{
  "pmid": "string — copy from input",
  "headline": "Plain-language present-tense headline, no jargon — punchy, counterintuitive, or surprising",
  "journal": "journal name",
  "pubdate": "publication date",
  "doi": "doi or empty string",
  "groundbreaking": "one or more of: Counterintuitive finding / Overturns prior research / First-in-class human study / Relevant women's health finding",
  "media_coverage": "{media_note}",
  "summary": "3 sentences max: what researchers did, who participated (N=X, age range if relevant, menopausal or reproductive status if relevant), key finding in plain language — verified against abstract",
  "why_it_matters": "1 sentence max. of real-world significance for women specifically. Do NOT imply clinical action.",
  "caveats": "comma-separated flags: small sample (N<100), observational design, single-center, self-reported outcomes, short follow-up, industry funding [name], preprint, secondary analysis, no sex-disaggregated data, animal or cell study — or 'None identified'",
  "fact_check_note": "Corrections only — if all claims match the abstract write empty string. No 'Abstract confirms...' phrasing.",
  "relevance_score": 7,
  "relevance_score_reason": "Max 15 words: topic fit and study quality.",
  "pitch_angles": [
    {{
      "publication_type": "e.g. Women's Health Magazine / Prevention / Self / Well+Good / The Cut / Shape / General health",
      "headline": "Publication-appropriate headline",
      "hook": "One sentence opening leading with the surprising or useful finding",
      "pitch_angle": "2 sentences max: what happened, why surprising or useful, lifestyle/wellness hook for women"
    }}
  ]
}}

Rules for pitch_angles:
- Generate ONE pitch angle if the study fits one obvious publication type
- Generate MULTIPLE angles (2-3 max) only when the study genuinely fits different audiences with meaningfully different framings — e.g. a menopause + mental health study could pitch differently to Prevention (symptom management angle) vs Well+Good (lifestyle/wellness angle) vs The Cut (cultural/societal angle)
- Do not pad with extra angles if one covers it

Rules for content:
- Verify sample size (N), study design, direction of effect against the abstract
- Flag explicitly if the study did not break down results by sex or gender — a critical caveat for women's health coverage
- For reproductive health: specify the population (pregnant, postpartum, perimenopausal, postmenopausal, reproductive age) — do not generalize across life stages
- For endocrinology: be clear about which hormones and conditions are studied (PCOS, thyroid, menopause, HRT) — never conflate them
- For psychiatry: flag whether findings are specific to women or from mixed-sex samples without female-specific analysis
- For nutrition: note whether dietary patterns or supplements were studied, and in what population
- Animal-only and cell-only studies should already be excluded by screening; if any slip through, clearly label and score lower
- Note the country/region of the study population when it's relevant to interpreting the finding. Flag (in caveats) when a result is closely tied to one non-US region's specific context — e.g. a soy-consumption pattern specific to rural China, or a water-quality issue specific to Iran — and unlikely to generalize to a US/global readership.
- Never use: breakthrough, cure, reverses, eliminates, proven to prevent
- Always use: suggests, found that, associated with, early evidence indicates
- No causal language for observational studies

relevance_score rubric (1–10): start at 5, then adjust:
  +2 counterintuitive or overturns prior belief
  +2 human subjects with female or majority-female sample (N≥100)
  +1 clear lifestyle/wellness hook for women
  +1 clean design (RCT, longitudinal, large cohort)
  +1 menopause, reproductive health, or women-specific condition
  −1 per major caveat
  −1 no sex-disaggregated data reported
  −2 animal or cell study only
  −2 finding is tied to a single non-US/non-multinational region's diet, genetics, environment, or healthcare system in a way unlikely to resonate with or apply to a US/global audience (this does not apply to large multinational cohorts, WHO/global-health studies, or findings with a clear universal biological mechanism)
  Topic fit bonus: menopause and HRT, PCOS, endometriosis, fertility, postpartum health, bone density, cardiovascular disease in women, female-specific cancers, eating disorders, perinatal mental health, contraception, sexual health score higher

{personalization}Return ONLY a valid JSON array, no other text.

Studies:
{studies_block}"""

    # Separate static instructions (cached across batches) from dynamic studies
    _sep = "\n\nStudies:\n"
    _ret = "\n\nReturn ONLY a valid JSON array, no other text."
    _system = prompt.split(_sep)[0].replace(_ret, "").strip() if _sep in prompt else ""
    _user = (
        f"Studies:\n{studies_block}\n\nReturn ONLY a valid JSON array, no other text."
        if _sep in prompt else prompt
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=6000,
        system=[{"type": "text", "text": _system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _user}],
    )
    try:
        results = extract_json(message.content[0].text)
        for i, r in enumerate(results):
            if not r.get("pmid"):
                r["pmid"] = batch[i]["pmid"]
        return results
    except Exception as e:
        print(f"  JSON parse error in process_batch: {e}")
        return [{"pmid": s["pmid"], "headline": s["title"], "journal": s["journal"],
                 "pubdate": s["pubdate"], "doi": s.get("doi", ""),
                 "groundbreaking": "Relevant women's health finding", "media_coverage": media_note,
                 "summary": "", "why_it_matters": "", "caveats": "",
                 "fact_check_note": "", "excluded": False,
                 "relevance_score": 5, "relevance_score_reason": "",
                 "pitch_angles": []} for s in batch]


# ── Step 7: Email notification ───────────────────────────────────────────────

def send_notification(category: str, chunk_label: str, study_count: int, run_date: str):
    subject = f"Women's Health Research Digest — {run_date} | {study_count} {'Study' if study_count == 1 else 'Studies'}"
    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Georgia,serif;max-width:500px;margin:auto;padding:24px;color:#222;">
<h2 style="color:#1a3a2e;">Women&#39;s Health Research Digest</h2>
<p><strong>{study_count} new {'study' if study_count == 1 else 'studies'}</strong> found in
<strong>{category}{chunk_label}</strong> — {run_date}</p>
<p>
  <a href="{DASHBOARD_URL}" style="display:inline-block;background:#2563eb;color:white;
  padding:10px 20px;border-radius:6px;text-decoration:none;font-size:15px;">
  View Dashboard →</a>
</p>
<p style="font-size:0.85em;color:#888;margin-top:2em;">
  Women&#39;s Health Research Digest · PubMed + Claude + SERPAPI
</p>
</body>
</html>"""
    try:
        r = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [RECIPIENT], "subject": subject, "html": html},
            timeout=20,
        )
        r.raise_for_status()
        print(f"Notification sent ✓  id={r.json().get('id', 'unknown')}")
    except Exception as e:
        print(f"Email error: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)
    start_str   = start.strftime("%Y/%m/%d")
    end_str     = now.strftime("%Y/%m/%d")
    run_date    = now.strftime("%b %d, %Y")

    print("=" * 60)
    print("Women's Health Research Digest")
    print(f"Coverage: {start.strftime('%b %d, %Y')} → {run_date}")
    print("=" * 60)

    categories = resolve_categories(CATEGORIES_INPUT)

    if FILTER_CATEGORY and FILTER_CATEGORY.lower() not in CATEGORIES_INPUT.lower():
        print(f"Skipping: filter='{FILTER_CATEGORY}', this job='{CATEGORIES_INPUT}' — no match.")
        return

    personalization = build_personalization(SOURCE_ID, DATA_DIR / "results.json")

    print(f"\nCategories: {', '.join(categories)}")
    issns = get_issns(categories)
    print(f"ISSNs: {len(issns)}")

    print(f"\nSearching PubMed...")
    pmids = pubmed_search(issns, start_str, end_str, TOPIC_FOCUS)
    print(f"PMIDs found: {len(pmids)}")

    print("\nFetching summaries...")
    summaries = fetch_summaries(pmids)
    candidates = screen_candidates(summaries)
    print(f"Candidates after screening: {len(candidates)}")

    media_checked = bool(SERPAPI_KEY)
    if media_checked:
        print(f"\nSERPAPI filter (threshold <{MEDIA_THRESHOLD})...")
        passed = apply_media_filter(candidates, MEDIA_THRESHOLD)
        if len(passed) < MIN_STUDIES:
            print(f"Only {len(passed)} passed — relaxing to <{MEDIA_RELAX}...")
            passed = apply_media_filter(candidates, MEDIA_RELAX)
    else:
        print("\nNo SERPAPI key — skipping media filter")
        passed = candidates

    print(f"After media filter: {len(passed)}")

    print("\nFetching abstracts...")
    studies = []
    for _, pmid, s in passed:
        abstract = fetch_abstract(pmid, doi=s.get("doi", ""))
        time.sleep(NCBI_DELAY)
        if not abstract:
            continue
        studies.append({
            "pmid": pmid, "title": s["title"], "journal": s["journal"],
            "pubdate": s["pubdate"], "doi": s.get("doi", ""), "abstract": abstract,
        })
        print(f"  {pmid} ✓")

    print(f"\nStudies with abstracts: {len(studies)}")

    if not studies:
        print("No studies — exiting.")
        return

    print(f"\nClaude pass ({CLAUDE_MODEL}) — writing, fact-checking, pitching...")
    enriched = []
    total_batches = (len(studies) + CLAUDE_BATCH_SIZE - 1) // CLAUDE_BATCH_SIZE
    for i in range(0, len(studies), CLAUDE_BATCH_SIZE):
        batch = studies[i : i + CLAUDE_BATCH_SIZE]
        print(f"  Batch {i//CLAUDE_BATCH_SIZE+1}/{total_batches}...")
        results = process_batch(batch, i + 1, media_checked, personalization)
        enriched.extend(results)

    enriched = [s for s in enriched if not s.get("excluded") and s.get("relevance_score", 0) >= MIN_RELEVANCE_SCORE]
    print(f"Included after exclusion + relevance-score (≥{MIN_RELEVANCE_SCORE}) check: {len(enriched)}")

    if not enriched:
        print("No included studies — exiting.")
        return

    chunk_label = f" ({CHUNK_INDEX}/{CHUNK_TOTAL})" if CHUNK_TOTAL > 1 else ""
    payload = {
        "run_date": now.strftime("%Y-%m-%d"),
        "run_timestamp": now.isoformat(),
        "category": CATEGORIES_INPUT,
        "chunk": f"{CHUNK_INDEX}/{CHUNK_TOTAL}",
        "coverage_days": DAYS_BACK,
        "journals_searched": len(issns),
        "media_checked": media_checked,
        "studies": enriched,
    }

    RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nResults saved to {RESULTS_PATH} ({len(enriched)} studies)")

    print("\nDone.")


if __name__ == "__main__":
    main()
