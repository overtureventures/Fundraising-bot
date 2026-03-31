"""
S-1 Prospector
Scans SEC EDGAR for recent S-1 filings, extracts principal stockholders,
matches against Affinity CRM, and outputs to console and CSV.
"""

import os
import logging
from datetime import datetime
from dotenv import load_dotenv

from edgar import get_recent_s1_filings, parse_stockholders
from propublica import lookup_foundation_officers
from affinity import AffinityClient
from output import write_to_csv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()


def classify_entity(name: str) -> str:
    name_lower = name.lower()
    if any(t in name_lower for t in ['foundation', 'endowment']):
        return 'foundation'
    if any(t in name_lower for t in ['family office', 'family trust', 'family lp']):
        return 'family_office'
    if any(t in name_lower for t in ['trust', 'estate']):
        return 'trust'
    if any(t in name_lower for t in ['capital', 'partners', 'ventures', 'fund',
                                      'management', 'advisors', 'llc', 'lp']):
        return 'fund'
    if any(t in name_lower for t in ['inc', 'corp', 'corporation', 'company']):
        return 'corporate'
    return 'unknown'


def generate_linkedin_search_url(name: str) -> str:
    encoded = name.replace(' ', '%20')
    return f'https://www.linkedin.com/search/results/companies/?keywords={encoded}'


def load_affinity_client():
    api_key = os.getenv('AFFINITY_API_KEY', '')
    if not api_key:
        logger.warning('AFFINITY_API_KEY not set. CRM matching will be skipped.')
        return None

    list_name = os.getenv('AFFINITY_LIST_NAME', 'Fundraising')
    client = AffinityClient(api_key)

    try:
        client.load_fundraising_list(list_name)
        logger.info(f'Affinity list "{list_name}" loaded successfully')
    except Exception as e:
        logger.error(f'Failed to load Affinity list: {e}')
        return None

    return client


def enrich_with_crm(investor: dict, client) -> dict:
    if client is None:
        return investor
    match = client.find_match(investor['investor_name'])
    if match:
        investor['in_crm'] = True
        investor['crm_status'] = match.get('status', '')
        investor['crm_last_activity'] = match.get('last_activity', '')
        investor['crm_notes'] = match.get('notes', '')
    return investor


def build_report(investors: list, run_date: str) -> str:
    """
    Build the full weekly report as a single string so it can be emitted
    via one logger.info call. This prevents Railway from interleaving the
    report with other log lines when stdout and stderr are mixed.
    """
    W = 100
    lines = ['', '=' * W, f'WEEKLY S-1 INVESTOR REPORT   {run_date}', '=' * W]

    if not investors:
        lines += ['   No investors found this week.', '=' * W]
        return '\n'.join(lines)

    by_company: dict = {}
    for inv in investors:
        by_company.setdefault(inv['company_ipo'], []).append(inv)

    for company, company_investors in by_company.items():
        lines += [
            '', '-' * W, company.upper(), '-' * W,
            f'Filing Date: {company_investors[0]["filing_date"]}',
            f'Investors Found: {len(company_investors)}',
            '',
        ]
        for i, inv in enumerate(company_investors, 1):
            crm_flag = '  [IN CRM]' if inv['in_crm'] else ''
            lines.append(f'{i}. {inv["investor_name"]}{crm_flag}')
            lines.append(f'   Type: {inv["entity_type"].replace("_", " ").title()}')

            details = []
            if inv.get('ownership_pct'):
                details.append(f'Ownership: {inv["ownership_pct"]}%')
            if inv.get('shares'):
                details.append(f'Shares: {inv["shares"]}')
            if details:
                lines.append(f'   {" | ".join(details)}')

            if inv['in_crm']:
                if inv.get('crm_status'):
                    lines.append(f'   CRM Status: {inv["crm_status"]}')
                if inv.get('crm_last_activity'):
                    lines.append(f'   Last Activity: {inv["crm_last_activity"]}')

            if inv.get('foundation_contacts'):
                lines.append(f'   Foundation Contacts: {inv["foundation_contacts"]}')

            lines.append(f'   LinkedIn: {inv["linkedin_search_url"]}')
            lines.append('')

    entity_counts: dict = {}
    for inv in investors:
        entity_counts[inv['entity_type']] = entity_counts.get(inv['entity_type'], 0) + 1

    lines += [
        '', '=' * W, 'WEEKLY SUMMARY', '=' * W,
        f'Total IPO Filings: {len(by_company)}',
        f'Total Investors Identified: {len(investors)}',
        f'Already in CRM: {sum(1 for i in investors if i["in_crm"])}',
        f'New Prospects: {sum(1 for i in investors if not i["in_crm"])}',
        '', 'Breakdown by Entity Type:',
    ]
    for entity_type, count in sorted(entity_counts.items(), key=lambda x: x[1], reverse=True):
        lines.append(f'   {entity_type.replace("_", " ").title()}: {count}')
    lines += ['', '=' * W]

    return '\n'.join(lines)


def main():
    logger.info('=' * 60)
    logger.info('Starting S-1 Prospector Weekly Run')
    logger.info('=' * 60)

    days_back = int(os.getenv('DAYS_BACK', 7))
    enrich_foundations = os.getenv('ENRICH_FOUNDATIONS', 'false').lower() == 'true'

    # Step 1: EDGAR filings
    logger.info(f'STEP 1: Fetching S-1 filings from the last {days_back} days...')
    filings = get_recent_s1_filings(days_back=days_back)
    logger.info(f'Found {len(filings)} S-1 filings')

    if not filings:
        logger.warning('No S-1 filings found in the specified time period')
        return []

    for filing in filings:
        logger.info(f'  {filing["company_name"]} (Filed: {filing["filing_date"]})')

    # Step 2: Parse stockholders
    logger.info('STEP 2: Parsing stockholder tables...')
    all_investors = []

    for i, filing in enumerate(filings, 1):
        logger.info(f'[{i}/{len(filings)}] {filing["company_name"]}')
        stockholders = parse_stockholders(filing)

        if stockholders:
            logger.info(f'  Found {len(stockholders)} stockholders')
        else:
            logger.warning(f'  No stockholders extracted')

        for stockholder in stockholders:
            all_investors.append({
                'investor_name': stockholder['name'],
                'company_ipo': filing['company_name'],
                'filing_date': filing['filing_date'],
                'ownership_pct': stockholder.get('ownership_pct', ''),
                'shares': stockholder.get('shares', ''),
                'entity_type': classify_entity(stockholder['name']),
                'in_crm': False,
                'crm_status': '',
                'crm_last_activity': '',
                'crm_notes': '',
                'foundation_contacts': '',
                'linkedin_search_url': generate_linkedin_search_url(stockholder['name']),
            })

    logger.info(f'Total investor records extracted: {len(all_investors)}')

    if not all_investors:
        logger.warning('No investors extracted from any filings')
        return []

    # Step 3: Affinity CRM matching
    logger.info('STEP 3: Matching against Affinity CRM...')
    affinity_client = load_affinity_client()
    all_investors = [enrich_with_crm(inv, affinity_client) for inv in all_investors]
    crm_matches = sum(1 for i in all_investors if i['in_crm'])
    logger.info(f'CRM matches: {crm_matches} of {len(all_investors)}')

    # Step 4: Foundation enrichment (off by default)
    if enrich_foundations:
        foundations = [i for i in all_investors if i['entity_type'] == 'foundation']
        if foundations:
            logger.info(f'STEP 4: Foundation 990 lookup ({len(foundations)} foundations)...')
            for inv in foundations:
                officers = lookup_foundation_officers(inv['investor_name'])
                if officers:
                    inv['foundation_contacts'] = '; '.join(
                        [f'{o["name"]} ({o["title"]})' for o in officers[:5]]
                    )
        else:
            logger.info('STEP 4: Skipped (no foundations found)')
    else:
        logger.info('STEP 4: Foundation enrichment disabled')

    # Step 5: Emit full report as single log entry (prevents Railway log interleaving)
    timestamp = datetime.now().strftime('%Y-%m-%d')
    logger.info(build_report(all_investors, timestamp))

    # Step 6: Save CSV
    filename = f's1_investors_{timestamp}.csv'
    write_to_csv(all_investors, filename)
    logger.info(f'Saved CSV to {filename}')

    logger.info('=' * 60)
    logger.info('RUN COMPLETE')
    logger.info(f'Filings Processed: {len(filings)}')
    logger.info(f'Investors Found: {len(all_investors)}')
    logger.info(f'In CRM: {sum(1 for i in all_investors if i["in_crm"])}')
    logger.info(f'New Prospects: {sum(1 for i in all_investors if not i["in_crm"])}')
    logger.info('=' * 60)

    return all_investors


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info('Run interrupted by user')
    except Exception as e:
        logger.error(f'FATAL ERROR: {e}', exc_info=True)
