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


def load_affinity_client() -> AffinityClient | None:
    """
    Instantiate and load the Affinity client if an API key is configured.
    Returns None if the key is missing so the run can continue without CRM matching.
    """
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


def enrich_with_crm(investor: dict, client: AffinityClient | None) -> dict:
    """
    Look up the investor in Affinity and populate CRM fields.
    Safe to call with client=None (returns unchanged investor).
    """
    if client is None:
        return investor

    match = client.find_match(investor['investor_name'])
    if match:
        investor['in_crm'] = True
        investor['crm_status'] = match.get('status', '')
        investor['crm_last_activity'] = match.get('last_activity', '')
        investor['crm_notes'] = match.get('notes', '')

    return investor


def print_results_to_console(investors: list, run_date: str):
    print('\n\n')
    print('=' * 100)
    print(f'WEEKLY S-1 INVESTOR REPORT   {run_date}')
    print('=' * 100)

    if not investors:
        print('   No investors found this week.')
        print('=' * 100)
        return

    by_company: dict[str, list] = {}
    for inv in investors:
        by_company.setdefault(inv['company_ipo'], []).append(inv)

    for company, company_investors in by_company.items():
        print(f'\n{"─" * 100}')
        print(f'{company.upper()}')
        print(f'{"─" * 100}')
        print(f'Filing Date: {company_investors[0]["filing_date"]}')
        print(f'Investors Found: {len(company_investors)}\n')

        for i, inv in enumerate(company_investors, 1):
            crm_flag = '  [IN CRM]' if inv['in_crm'] else ''
            print(f'{i}. {inv["investor_name"]}{crm_flag}')
            print(f'   Type: {inv["entity_type"].replace("_", " ").title()}')

            details = []
            if inv.get('ownership_pct'):
                details.append(f'Ownership: {inv["ownership_pct"]}%')
            if inv.get('shares'):
                details.append(f'Shares: {inv["shares"]}')
            if details:
                print(f'   {" | ".join(details)}')

            if inv['in_crm']:
                if inv.get('crm_status'):
                    print(f'   CRM Status: {inv["crm_status"]}')
                if inv.get('crm_last_activity'):
                    print(f'   Last Activity: {inv["crm_last_activity"]}')

            if inv.get('foundation_contacts'):
                print(f'   Foundation Contacts: {inv["foundation_contacts"]}')

            print(f'   LinkedIn: {inv["linkedin_search_url"]}')
            print()

    print('\n' + '=' * 100)
    print('WEEKLY SUMMARY')
    print('=' * 100)
    print(f'\nTotal IPO Filings: {len(by_company)}')
    print(f'Total Investors Identified: {len(investors)}')
    print(f'Already in CRM: {sum(1 for i in investors if i["in_crm"])}')
    print(f'New Prospects: {sum(1 for i in investors if not i["in_crm"])}\n')

    print('Breakdown by Entity Type:')
    entity_counts: dict[str, int] = {}
    for inv in investors:
        entity_counts[inv['entity_type']] = entity_counts.get(inv['entity_type'], 0) + 1
    for entity_type, count in sorted(entity_counts.items(), key=lambda x: x[1], reverse=True):
        print(f'   {entity_type.replace("_", " ").title()}: {count}')

    print('\n' + '=' * 100)


def main():
    logger.info('=' * 60)
    logger.info('Starting S-1 Prospector Weekly Run')
    logger.info('=' * 60)

    days_back = int(os.getenv('DAYS_BACK', 7))
    enrich_foundations = os.getenv('ENRICH_FOUNDATIONS', 'false').lower() == 'true'

    # Step 1: EDGAR filings
    logger.info(f'\nSTEP 1: Fetching S-1 filings from the last {days_back} days...')
    filings = get_recent_s1_filings(days_back=days_back)
    logger.info(f'Found {len(filings)} S-1 filings')

    if not filings:
        logger.warning('No S-1 filings found in the specified time period')
        return []

    for filing in filings:
        logger.info(f'  {filing["company_name"]} (Filed: {filing["filing_date"]})')

    # Step 2: Parse stockholders
    logger.info('\nSTEP 2: Parsing stockholder tables...')
    all_investors = []

    for i, filing in enumerate(filings, 1):
        logger.info(f'[{i}/{len(filings)}] Processing: {filing["company_name"]}')
        stockholders = parse_stockholders(filing)

        if stockholders:
            logger.info(f'  Found {len(stockholders)} stockholders')
        else:
            logger.warning(f'  No stockholders extracted from {filing["company_name"]}')

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

    logger.info(f'\nTotal investor records extracted: {len(all_investors)}')

    if not all_investors:
        logger.warning('No investors extracted from any filings')
        return []

    # Step 3: Affinity CRM matching
    logger.info('\nSTEP 3: Matching against Affinity CRM...')
    affinity_client = load_affinity_client()
    all_investors = [enrich_with_crm(inv, affinity_client) for inv in all_investors]

    crm_matches = sum(1 for i in all_investors if i['in_crm'])
    logger.info(f'CRM matches found: {crm_matches} of {len(all_investors)}')

    # Step 4: Foundation enrichment (off by default, limited API value)
    if enrich_foundations:
        foundations = [i for i in all_investors if i['entity_type'] == 'foundation']
        if foundations:
            logger.info(f'\nSTEP 4: Foundation 990 lookup ({len(foundations)} foundations)...')
            for inv in foundations:
                officers = lookup_foundation_officers(inv['investor_name'])
                if officers:
                    contacts = '; '.join(
                        [f'{o["name"]} ({o["title"]})' for o in officers[:5]]
                    )
                    inv['foundation_contacts'] = contacts
        else:
            logger.info('\nSTEP 4: Skipped (no foundations found)')
    else:
        logger.info('\nSTEP 4: Foundation enrichment disabled (set ENRICH_FOUNDATIONS=true to enable)')

    # Step 5: Print and save
    timestamp = datetime.now().strftime('%Y-%m-%d')
    print_results_to_console(all_investors, timestamp)

    filename = f's1_investors_{timestamp}.csv'
    write_to_csv(all_investors, filename)
    logger.info(f'Saved CSV to {filename}')

    logger.info('\n' + '=' * 60)
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
