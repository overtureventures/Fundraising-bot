"""
Filter and enrichment logic for the S-1 Prospector.

Filtering pipeline (in order):
  1. Exclude entire filings from SPAC / blank-check companies
  2. Exclude entire filings from life science / biotech companies
  3. Strip junk aggregate rows (already handled upstream in edgar.py)
  4. For remaining investors: classify as institutional entity or individual
  5. For institutional entities: cross-reference EDGAR 13F filers to confirm
     they are registered as an institutional investment manager ($100M+ AUM)
  6. Individuals always pass through — they are potential HNWI LP prospects

The final output is a list of investors with an added 'lp_qualified' flag
and 'investor_class' field ('institutional' or 'individual').
"""

import re
import time
import requests
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Reuse SEC headers from edgar module
import os
_user_agent_email = os.getenv('SEC_USER_AGENT_EMAIL', 'contact@yourfirm.com')
SEC_HEADERS = {
    'User-Agent': f'S1Prospector/1.0 ({_user_agent_email})',
    'Accept-Encoding': 'gzip, deflate'
}

EDGAR_COMPANY_SEARCH = 'https://efts.sec.gov/LATEST/search-index'
EDGAR_SUBMISSIONS_URL = 'https://data.sec.gov/submissions'


# ---------------------------------------------------------------------------
# Filing-level exclusion: SPACs and blank-check companies
# ---------------------------------------------------------------------------

SPAC_PATTERNS = re.compile(
    r'\b(acquisition corp|acquisition corporation|blank check|special purpose'
    r'|blank\s+check\s+company)\b',
    re.I
)

def is_spac_filing(company_name: str) -> bool:
    """Return True if the filing is from a SPAC or blank-check vehicle."""
    return bool(SPAC_PATTERNS.search(company_name))


# ---------------------------------------------------------------------------
# Filing-level exclusion: life science and biotech companies
# These investors will not allocate to a climate/energy/AI/resilience fund
# ---------------------------------------------------------------------------

BIOTECH_NAME_PATTERNS = re.compile(
    r'\b(therapeutics|biosciences|bioscience|biologics|biologic|pharma|pharmaceutical'
    r'|genomics|oncology|biopharma|biotech|clinical|diagnostics|medtech'
    r'|medical devices|life sciences|lifesciences|health sciences'
    r'|neuroscience|immunology|radiology)\b',
    re.I
)

# Also catch company names starting with Bio (e.g. BioSqueeze, FibroBiologics, LeonaBio)
BIOTECH_PREFIX_PATTERN = re.compile(r'bio(?:logics|sciences|logy|tech|med|pharma|squeeze)', re.I)
BIOTECH_SUFFIX_PATTERN = re.compile(r'\w+bio\b', re.I)

# SIC codes for biotech (283x), pharma (283x), medical devices (384x),
# hospitals/health services (8000-8099)
BIOTECH_SIC_RANGES = [
    (2830, 2836),  # pharmaceutical preparations
    (3841, 3851),  # medical instruments and devices
    (8000, 8099),  # health services
    (2860, 2869),  # industrial chemicals (some biotech)
    (8700, 8742),  # research and testing
]

def _sic_is_biotech(sic: int) -> bool:
    for lo, hi in BIOTECH_SIC_RANGES:
        if lo <= sic <= hi:
            return True
    return False

def get_company_sic(cik_padded: str) -> Optional[int]:
    """Fetch the SIC code for a company from the EDGAR submissions endpoint."""
    url = f'{EDGAR_SUBMISSIONS_URL}/CIK{cik_padded}.json'
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        r.raise_for_status()
        sic_str = r.json().get('sic', '')
        return int(sic_str) if sic_str else None
    except Exception:
        return None

def is_biotech_filing(company_name: str, cik_padded: str = '') -> bool:
    """
    Return True if the filing is from a life science or biotech company.
    Checks company name first (fast), then SIC code if cik_padded is provided.
    """
    if BIOTECH_NAME_PATTERNS.search(company_name):
        return True
    if BIOTECH_PREFIX_PATTERN.search(company_name):
        return True
    if BIOTECH_SUFFIX_PATTERN.search(company_name):
        return True
    if cik_padded:
        sic = get_company_sic(cik_padded)
        if sic and _sic_is_biotech(sic):
            return True
    return False


# ---------------------------------------------------------------------------
# Investor-level classification: institutional entity vs individual
# ---------------------------------------------------------------------------

INSTITUTIONAL_INDICATORS = re.compile(
    r'\b(llc|l\.l\.c|lp|l\.p\.|llp|capital|partners|ventures|venture'
    r'|management|advisors|adviser|investment|investments|fund|funds'
    r'|holdings|holding|group|asset|assets|securities|equity|trust'
    r'|foundation|endowment|family office|associates|partnership'
    r'|limited|ltd|inc\b|corp\b|corporation|company|co\b'
    r'|gp\b|pe\b|vc\b|financial|wealth|portfolio)\b',
    re.I
)

def classify_investor(name: str) -> str:
    """
    Return 'institutional' if the name looks like a fund/entity,
    'individual' if it looks like a person's name.
    """
    if INSTITUTIONAL_INDICATORS.search(name):
        return 'institutional'
    # Looks like a person: 2-4 words, each starting with a capital
    words = name.split()
    if 2 <= len(words) <= 5:
        cap_words = sum(1 for w in words if w and w[0].isupper() and not w.isupper())
        if cap_words >= 2:
            return 'individual'
    return 'institutional'  # default to institutional if ambiguous


# ---------------------------------------------------------------------------
# 13F cross-reference: verify institutional managers on EDGAR
# ---------------------------------------------------------------------------

def check_13f_filer(entity_name: str) -> bool:
    """
    Search EDGAR for 13F filings from this entity.
    Returns True if a matching 13F filer is found (confirms $100M+ AUM).
    Uses the EDGAR full-text search with form type filter.
    """
    # Clean the name for searching: strip legal suffixes for a broader match
    search_name = re.sub(
        r'\b(llc|lp|l\.p\.|l\.l\.c|inc|corp|ltd|limited|llp)\b\.?',
        '', entity_name, flags=re.I
    ).strip().strip(',').strip()

    # Use only the first 3-4 meaningful words to avoid over-constraining
    words = search_name.split()
    query = ' '.join(words[:4]) if len(words) > 4 else search_name

    if len(query) < 3:
        return False

    try:
        params = {
            'q': f'"{query}"',
            'forms': '13F-HR,13F-HR/A',
            'dateRange': 'custom',
            'startdt': '2020-01-01',
            'enddt': '2026-12-31',
        }
        r = requests.get(
            EDGAR_COMPANY_SEARCH,
            params=params,
            headers=SEC_HEADERS,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        hits = data.get('hits', {}).get('hits', [])
        if hits:
            logger.debug(f'13F match for "{entity_name}": {hits[0].get("_source", {}).get("display_names", "")}')
            return True
    except Exception as e:
        logger.debug(f'13F check failed for "{entity_name}": {e}')

    return False


# ---------------------------------------------------------------------------
# Company description: pull one-liner from the S-1 filing text
# ---------------------------------------------------------------------------

def get_company_description(filing: dict) -> str:
    """
    Extract a short company description from the S-1 cover page text.
    Looks for the business description sentence that typically appears
    near the top of the prospectus.
    Falls back to a generic description if nothing useful is found.
    """
    from edgar import get_s1_document_url
    import requests
    from bs4 import BeautifulSoup

    cik = filing.get('cik', '')
    cik_padded = filing.get('cik_padded', cik.zfill(10))
    accession_clean = filing.get('accession_clean', '')

    doc_url = get_s1_document_url(cik, cik_padded, accession_clean)
    if not doc_url:
        return ''

    if '/ix?doc=' in doc_url:
        doc_url = 'https://www.sec.gov' + doc_url.split('/ix?doc=')[1]

    try:
        r = requests.get(doc_url, headers=SEC_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')

        # S-1 cover pages typically have a prospectus summary section
        # that describes the business in one or two sentences
        text = soup.get_text(' ', strip=True)

        # Look for "we are a", "the company is a", "X is a [descriptor] company"
        patterns = [
            r'[Ww]e are (?:a |an )(?!large|accelerated|smaller|emerging|non-).{20,200}?[.;]',
            r'[Ww]e develop.{10,200}?[.;]',
            r'[Ww]e design.{10,200}?[.;]',
            r'[Ww]e provide.{10,200}?[.;]',
            r'[Ww]e operate.{10,200}?[.;]',
            r'[Ww]e build.{10,200}?[.;]',
            r'[Ww]e offer.{10,200}?[.;]',
            r'[Ww]e create.{10,200}?[.;]',
        ]

        # SEC boilerplate to reject — these appear in every S-1 cover page
        BOILERPLATE = re.compile(
            r'(accelerated filer|smaller reporting company|emerging growth company'
            r'|controlled company|check mark|indicate by check|annual report'
            r'|commission file|exchange act|securities act)',
            re.I
        )

        # Real business descriptions are in the Prospectus Summary,
        # typically 5k-40k chars into the document. Search a broader window
        # and reject any match that contains boilerplate language.
        search_window = text[3000:50000]

        for pattern in patterns:
            for m in re.finditer(pattern, search_window):
                desc = m.group(0).strip()
                if BOILERPLATE.search(desc):
                    continue
                if len(desc) < 40:
                    continue
                if len(desc) > 120:
                    desc = desc[:117] + '...'
                return desc

    except Exception as e:
        logger.debug(f'Description fetch failed for {filing.get("company_name", "")}: {e}')

    return ''


# ---------------------------------------------------------------------------
# Main filter pipeline
# ---------------------------------------------------------------------------

def filter_filings(filings: list) -> list:
    """
    Remove SPAC and biotech filings from the list before parsing.
    Returns the filtered list with exclusion reason logged.
    """
    kept = []
    for filing in filings:
        name = filing.get('company_name', '')
        cik_padded = filing.get('cik_padded', '')

        if is_spac_filing(name):
            logger.info(f'EXCLUDED (SPAC): {name}')
            continue

        if is_biotech_filing(name, cik_padded):
            logger.info(f'EXCLUDED (biotech/life science): {name}')
            continue

        kept.append(filing)

    logger.info(f'Filing filter: {len(filings)} in -> {len(kept)} kept')
    return kept


def qualify_investors(investors: list) -> list:
    """
    For each investor:
    - Classify as 'institutional' or 'individual'
    - For institutional: run 13F check, set lp_qualified=True if verified
      or if name has strong institutional signals even without 13F hit
    - For individuals: always set lp_qualified=True
    Returns only lp_qualified investors.
    """
    qualified = []

    for inv in investors:
        name = inv.get('investor_name', '')
        investor_class = classify_investor(name)
        inv['investor_class'] = investor_class

        if investor_class == 'individual':
            inv['lp_qualified'] = True
            inv['verified_13f'] = False
            qualified.append(inv)
            continue

        # Institutional: run 13F check
        verified = check_13f_filer(name)
        inv['verified_13f'] = verified

        if verified:
            inv['lp_qualified'] = True
            logger.info(f'13F verified: {name}')
        else:
            # Fall back to name heuristics: strong institutional signals qualify
            strong_signals = re.compile(
                r'\b(capital|partners|ventures|management|advisors|fund|funds'
                r'|endowment|foundation|asset management|investment|securities'
                r'|equity|holdings|wealth|portfolio|family office)\b',
                re.I
            )
            if strong_signals.search(name):
                inv['lp_qualified'] = True
                logger.info(f'Qualified by name signal (no 13F): {name}')
            else:
                inv['lp_qualified'] = False
                logger.debug(f'Not qualified: {name}')

        # Rate limit: be gentle with EDGAR
        time.sleep(0.2)

        if inv['lp_qualified']:
            qualified.append(inv)

    logger.info(f'Investor filter: {len(investors)} in -> {len(qualified)} qualified')
    return qualified
