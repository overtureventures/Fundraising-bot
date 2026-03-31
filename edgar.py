"""
SEC EDGAR API integration for fetching and parsing S-1 filings.
Uses the EFTS full-text search API with date range filtering and
constructs document URLs from the submissions JSON rather than scraping.
"""

import os
import re
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
import logging

logger = logging.getLogger(__name__)

# Read from env so Railway can set a real contact email
_user_agent_email = os.getenv('SEC_USER_AGENT_EMAIL', 'contact@yourfirm.com')
SEC_HEADERS = {
    'User-Agent': f'S1Prospector/1.0 ({_user_agent_email})',
    'Accept-Encoding': 'gzip, deflate'
}

EDGAR_SUBMISSIONS_URL = 'https://data.sec.gov/submissions'
EDGAR_ARCHIVES_URL = 'https://www.sec.gov/Archives/edgar/data'
EFTS_SEARCH_URL = 'https://efts.sec.gov/LATEST/search-index'


def get_recent_s1_filings(days_back: int = 7) -> List[Dict]:
    """
    Fetch S-1 and S-1/A filings from the last N days using the EFTS
    full-text search API with explicit date range parameters.
    Falls back to the RSS feed if EFTS returns nothing.
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    filings = _fetch_via_efts(start_str, end_str)
    if not filings:
        logger.warning('EFTS returned no results, falling back to RSS feed')
        filings = _fetch_via_rss(start_date)

    logger.info(f'Found {len(filings)} S-1 filings between {start_str} and {end_str}')
    return filings


def _fetch_via_efts(start_str: str, end_str: str) -> List[Dict]:
    """Query the EFTS search API with a date range."""
    filings = []
    try:
        params = {
            'forms': 'S-1,S-1/A',
            'dateRange': 'custom',
            'startdt': start_str,
            'enddt': end_str,
            'hits.hits.total.value': 1,
            'hits.hits._source.period_of_report': 1,
        }
        response = requests.get(EFTS_SEARCH_URL, params=params, headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()

        hits = data.get('hits', {}).get('hits', [])
        for hit in hits:
            source = hit.get('_source', {})

            # EFTS native API field names (efts.sec.gov)
            # entity_name, file_date, period_of_report, form_type, file_num
            # display_names is an array of "Name (CIK)" strings
            display_names = source.get('display_names', [])
            entity_name = source.get('entity_name', '')
            if not entity_name and display_names:
                # display_names entries look like "Acme Corp (0001234567) (CIK)"
                entity_name = display_names[0].split('(')[0].strip()

            form_type = source.get('form_type', 'S-1')
            file_date = source.get('file_date', '')

            # Extract CIK from display_names if not directly available
            cik_raw = source.get('entity_id', '')
            if not cik_raw and display_names:
                cik_match = re.search(r'\((\d{10})\)', display_names[0])
                if cik_match:
                    cik_raw = cik_match.group(1)

            # _id is the accession number with dashes e.g. 0001193125-24-012345
            accession_dashed = hit.get('_id', '')
            accession_clean = accession_dashed.replace('-', '')

            if not cik_raw or not accession_clean:
                continue

            cik_stripped = str(cik_raw).lstrip('0')
            cik_padded = str(cik_raw).zfill(10)

            filings.append({
                'form_type': form_type,
                'company_name': entity_name.strip(),
                'cik': cik_stripped,
                'cik_padded': cik_padded,
                'accession_clean': accession_clean,
                'filing_date': file_date,
                # Direct link to the filing index
                'filing_url': (
                    f'https://www.sec.gov/cgi-bin/browse-edgar'
                    f'?action=getcompany&CIK={cik_stripped}&type=S-1&dateb=&owner=include&count=10'
                ),
            })

    except requests.RequestException as e:
        logger.error(f'EFTS query failed: {e}')

    return filings


def _fetch_via_rss(start_date: datetime) -> List[Dict]:
    """
    Fallback: parse the EDGAR RSS feed and filter by date client-side.
    The feed returns the most recent 100 S-1s regardless of date.
    """
    filings = []
    try:
        rss_url = (
            'https://www.sec.gov/cgi-bin/browse-edgar'
            '?action=getcurrent&type=S-1&company=&dateb=&owner=include&count=100&output=atom'
        )
        response = requests.get(rss_url, headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'xml')
        for entry in soup.find_all('entry'):
            title_tag = entry.find('title')
            updated_tag = entry.find('updated')
            link_tag = entry.find('link')

            if not title_tag or not updated_tag:
                continue

            title = title_tag.text
            link = link_tag['href'] if link_tag else ''

            match = re.match(r'(S-1(?:/A)?) - (.+?) \((\d+)\)', title)
            if not match:
                continue

            form_type = match.group(1)
            company_name = match.group(2).strip()
            cik = match.group(3)

            try:
                filing_date = datetime.fromisoformat(updated_tag.text.replace('Z', '+00:00'))
            except ValueError:
                filing_date = datetime.now()

            if filing_date.replace(tzinfo=None) < start_date:
                continue

            filings.append({
                'form_type': form_type,
                'company_name': company_name,
                'cik': cik.lstrip('0'),
                'cik_padded': cik.zfill(10),
                'accession_clean': '',  # not available from RSS directly
                'filing_date': filing_date.strftime('%Y-%m-%d'),
                'filing_url': link,
            })

    except requests.RequestException as e:
        logger.error(f'RSS fallback failed: {e}')

    return filings


def get_accession_number(cik_padded: str, form_type: str = 'S-1') -> Optional[str]:
    """
    Fetch the most recent S-1 accession number for a company from the
    submissions JSON. Returns the accession number without dashes.
    """
    url = f'{EDGAR_SUBMISSIONS_URL}/CIK{cik_padded}.json'
    try:
        response = requests.get(url, headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()

        recent = data.get('filings', {}).get('recent', {})
        forms = recent.get('form', [])
        accessions = recent.get('accessionNumber', [])

        for form, accession in zip(forms, accessions):
            if form in ('S-1', 'S-1/A'):
                return accession.replace('-', '')

    except requests.RequestException as e:
        logger.error(f'Error fetching submissions for CIK {cik_padded}: {e}')

    return None


def get_s1_document_url(cik: str, cik_padded: str, accession_clean: str) -> Optional[str]:
    """
    Build the URL to the primary S-1 HTML document using the filing index JSON.
    This is deterministic and does not require HTML scraping.
    """
    if not accession_clean:
        accession_clean = get_accession_number(cik_padded)
    if not accession_clean:
        logger.warning(f'No accession number found for CIK {cik}')
        return None

    index_url = f'{EDGAR_ARCHIVES_URL}/{cik}/{accession_clean}/index.json'
    try:
        response = requests.get(index_url, headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()
        index_data = response.json()

        items = index_data.get('directory', {}).get('item', [])

        # Prefer a file whose name contains 's-1' or 'prospectus'
        for item in items:
            name = item.get('name', '').lower()
            if name.endswith('.htm') and ('s-1' in name or 'prospectus' in name):
                return f'{EDGAR_ARCHIVES_URL}/{cik}/{accession_clean}/{item["name"]}'

        # Fallback: first .htm file
        for item in items:
            if item.get('name', '').endswith('.htm'):
                return f'{EDGAR_ARCHIVES_URL}/{cik}/{accession_clean}/{item["name"]}'

    except requests.RequestException as e:
        logger.error(f'Error fetching index for {cik}/{accession_clean}: {e}')

    return None


def parse_stockholders(filing: Dict) -> List[Dict]:
    """
    Fetch the S-1 document and extract the principal stockholders table.
    Uses the index JSON to find the correct document URL rather than scraping links.
    """
    cik = filing.get('cik', '')
    cik_padded = filing.get('cik_padded', cik.zfill(10))
    accession_clean = filing.get('accession_clean', '')

    doc_url = get_s1_document_url(cik, cik_padded, accession_clean)
    if not doc_url:
        logger.warning(f'Could not resolve document URL for {filing["company_name"]}')
        return []

    logger.info(f'Fetching S-1 document: {doc_url}')
    try:
        response = requests.get(doc_url, headers=SEC_HEADERS, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f'Failed to fetch S-1 for {filing["company_name"]}: {e}')
        return []

    doc_soup = BeautifulSoup(response.content, 'html.parser')
    stockholders = extract_stockholder_table(doc_soup)
    logger.info(f'Extracted {len(stockholders)} stockholders from {filing["company_name"]}')
    return stockholders


def is_valid_investor_name(name: str) -> bool:
    """
    Return True if the string looks like a real investor name rather than
    a section header, table caption, or other non-name content.
    """
    name_lower = name.lower().strip()
    name_upper = name.upper().strip()

    if len(name) < 3 or len(name) > 150:
        return False

    words = name.split()
    if name == name_upper and len(words) > 4:
        return False

    exact_rejects = [
        'directors and executive officers',
        'executive officers and directors',
        'principal shareholders',
        'common stock',
        'class a common stock',
        'class b common stock',
        'preferred stock',
    ]
    name_no_pct = re.sub(r'\s*\([\d\.]+%\)\s*', '', name_lower).strip()
    name_no_pct = re.sub(r'\s*\(more than \d+%\).*', '', name_no_pct).strip()
    name_no_pct = name_no_pct.rstrip(':')
    if name_no_pct in exact_rejects:
        return False

    section_headers = [
        'use of proceeds', 'plan of distribution', 'risk factors',
        'legal matters', 'experts', 'underwriting', 'indemnification',
        'available information', 'financial statements', 'part i',
        'part ii', 'item ', 'where you can find', 'material u.s.',
        'certain relationships', 'related transactions', 'description of',
        'dilution', 'capitalization', 'executive compensation',
        'security ownership', 'principal stockholders', 'selling stockholders',
        'shares eligible', 'tax considerations', 'erisa considerations',
        'validity of', 'legal proceedings', 'market for', 'dividend policy',
        'selected financial', 'properties', 'directors and officers',
        'table of contents', 'prospectus summary', 'the offering',
        'corporate governance', 'related party', 'beneficial owner',
        '5% or greater', 'information not required', 'limitation on',
        'release of funds', 'nasdaq', 'nyse', 'trading symbol',
        'disclosure of', 'commission position', 'index to', 'changes in',
        'disagreements with', 'accountants on', 'accounting and',
        'federal tax', 'non-u.s. holders', 'securities act',
        'forward-looking', 'summary of', 'overview', 'background',
        'how to', 'what is', 'why we', 'our business', 'our company',
        'financial condition', 'results of operations', 'liquidity',
        'critical accounting', 'recent developments', 'industry',
        'competition', 'intellectual property', 'government regulation',
        'employees', 'facilities', 'legal proceedings',
        'principal shareholders', 'more than 5%', 'class a', 'class b',
        'common stock', 'preferred stock', 'series a', 'series b',
    ]
    for header in section_headers:
        if header in name_lower:
            return False

    bad_starts = [
        'name', 'total', '(', '_', '*', '-', '\u2014', 'note', 'see ',
        'the ', 'our ', 'we ', 'an ', 'a ', 'all executive', 'all directors',
        'officers and directors as a group', 'executive officers and directors',
        'item', 'part', 'section', 'article', 'exhibit', 'schedule',
        'index', 'table', 'summary', 'overview', 'introduction',
        'directors and', 'executive officers',
    ]
    for start in bad_starts:
        if name_lower.startswith(start):
            if 'as a group' in name_lower and any(c.isdigit() for c in name):
                return True
            return False

    if re.match(r'^[\d\s\.\,\%\$\(\)\-]+$', name):
        return False
    if re.match(r'^\(\d+\)', name) or re.match(r'^\*+', name):
        return False

    bad_endings = ['statements', 'information', 'considerations', 'matters',
                   'disclosure', 'liability', 'liabilities']
    for ending in bad_endings:
        if name_lower.endswith(ending):
            return False

    entity_indicators = [
        'llc', 'llp', 'l.l.c', 'l.p.', ' lp', 'inc', 'corp', 'corporation',
        'fund', 'capital', 'partners', 'venture', 'trust', 'holdings',
        'management', 'advisors', 'investment', 'equity', 'group',
        'foundation', 'endowment', 'family', 'associates', 'asset',
        'securities', 'limited', 'ltd', 'company', 'co.', ' gp',
        'partnership',
    ]
    has_entity_indicator = any(ind in name_lower for ind in entity_indicators)

    capitalized_words = sum(1 for w in words if w and w[0].isupper() and not w.isupper())
    looks_like_person = 2 <= len(words) <= 5 and capitalized_words >= 2 and name != name_upper

    has_credentials = bool(re.search(r'\b(Ph\.?D|M\.?D|M\.?B\.?A|J\.?D|CPA|CFA)\b', name, re.I))

    if re.search(r'\d+\.?\d*%', name):
        if has_entity_indicator or looks_like_person or has_credentials:
            return True

    if has_entity_indicator or looks_like_person or has_credentials:
        return True

    if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w):
        return True

    return False


def extract_stockholder_table(soup: BeautifulSoup) -> List[Dict]:
    """
    Find and parse the principal stockholders table from a parsed S-1 document.
    """
    stockholders = []

    header_patterns = [
        r'principal\s+(and\s+selling\s+)?stockholders',
        r'security\s+ownership',
        r'beneficial\s+owner',
        r'selling\s+stockholders',
        r'principal\s+shareholders',
    ]

    tables = soup.find_all('table')

    for table in tables:
        table_text = table.get_text().lower()
        preceding_text = ''

        for prev in table.find_all_previous(limit=5):
            if prev.name in ['h1', 'h2', 'h3', 'h4', 'p', 'div', 'b', 'strong']:
                preceding_text = prev.get_text().lower()
                break

        is_stockholder_table = False
        for pattern in header_patterns:
            if re.search(pattern, table_text) or re.search(pattern, preceding_text):
                is_stockholder_table = True
                break

        if 'beneficial' in table_text and ('shares' in table_text or 'percent' in table_text):
            is_stockholder_table = True

        if not is_stockholder_table:
            continue

        rows = table.find_all('tr')
        header_row_idx = None
        for idx, row in enumerate(rows):
            row_text = row.get_text().lower()
            if ('name' in row_text and ('shares' in row_text or 'percent' in row_text)) or \
               ('beneficial owner' in row_text):
                header_row_idx = idx
                break

        if header_row_idx is None:
            continue

        for row in rows[header_row_idx + 1:]:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2:
                continue

            name_cell = cells[0].get_text().strip()
            name = re.sub(r'\s+', ' ', name_cell)
            name = re.sub(r'\(\d+\)', '', name)
            name = re.sub(r'\([a-z]\)', '', name, flags=re.I)
            name = name.strip()

            if not is_valid_investor_name(name):
                continue

            stockholder = {'name': name}

            for cell in cells[1:]:
                cell_text = cell.get_text().strip()

                pct_match = re.search(r'(\d+\.?\d*)%', cell_text)
                if pct_match and 'ownership_pct' not in stockholder:
                    stockholder['ownership_pct'] = pct_match.group(1)

                share_match = re.search(r'([\d,]+)', cell_text.replace('%', ''))
                if share_match and 'shares' not in stockholder:
                    shares = share_match.group(1).replace(',', '')
                    if shares.isdigit() and int(shares) > 100:
                        stockholder['shares'] = shares

            stockholders.append(stockholder)

        if stockholders:
            break

    return stockholders
