"""
Follow-Up Bot

Takes a processed Granola note context dict, looks up the LP in Affinity,
calls Claude with the full fundraising tactics prompt, and posts the result
to #fundraising-bot on Slack, tagging the GP who had the call.
"""

import os
import logging
import requests
from typing import Dict, Optional
from affinity import AffinityClient

logger = logging.getLogger(__name__)

FUNDRAISING_BOT_CHANNEL = "C0AQHP58A0Z"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
SLACK_API_URL = "https://slack.com/api"

TACTICS_CONTEXT = """
You are the fundraising intelligence system for Overture Ventures, a climate-focused
early-stage venture fund raising Fund II.

MALTBY BUCKET CLASSIFICATION:
- REAL SHOT: notes contain "fits our mandate" / "next steps" / "send materials" / "interested"
- NEEDS WORK: notes contain "interesting" / "still getting our arms around" / "TBD" / no clear next step
- STRUCTURAL NO: notes contain "already in X funds" / "capacity issue" / "timing" / "I like you but..."

FOLLOW-UP ACTION BY BUCKET:
- REAL SHOT: Deal update or fund momentum email. Specific next step (IC materials, reference call,
  data room link). Cadence: every 2 to 3 days.
- NEEDS WORK: Show dont tell email. Frame as "You mentioned X, here is a specific proof point."
  Conclude with a proposed next meeting. Cadence: every 2 to 3 weeks.
- STRUCTURAL NO: No ask value add only. No pitch reference. Cadence: quarterly.

LP TYPE PERSONALIZATION:
- Institutional: Formal. IC ready materials. Never pressure.
- Family office: Personal and direct. Text or personal email preferred.
- HNWI: Personal conviction and relationship.
- Fund of funds: Deep transparency. Differentiation is everything.

WARM UP RULE: Never make a capital ask without 2 to 3 value add touchpoints first.

OVERTURE PORTFOLIO FOR MATCHING:
Use the LP's stated sector interests from the call notes to suggest a specific portfolio company
touchpoint in the follow-up. Match on these:

ENERGY TRANSITION:
- Antora: Thermal batteries cheaper than natural gas. Pre-IPO scale. Co-investors: Breakthrough Energy, Lowercarbon.
- Blue Energy: Small modular nuclear power plants. 12x markup. Co-investors: At One, The Engine.
- Hexium: Advanced nuclear fuels, accelerating uranium enrichment. Series A raising Q2/Q3.
- Crux: Clean energy tax credit marketplace.
- GridCARE: AI-powered grid capacity. Series A at $200M post-money, verbal term sheet.
- Halcyon: AI platform for energy data. Series A closed at $92.5M post. Customers include Google, Meta, BlackRock, NextEra.
- Bedrock Energy: Advanced geothermal heating and cooling. 1.46x markup.
- Moment Energy: Used battery systems. Series B at $165M post, 2.17x markup.
- Lydian: Sustainable Aviation Fuel. Series A led by Breakthrough Energy Ventures.
- Recheck: Grid compliance and monitoring.
- Blumen: Policy tech for energy funding.
- Zero Homes: Home electrification. $17M Series A led by Prelude Ventures.

RESILIENCE:
- BurnBot: Autonomous wildfire prevention robots. RX2 machines in field, $500K Cal Fire contract.
- Earth Force: Software for US Forest Service vegetation management. Default USFS platform, $50M+ contracted.
- Floodbase: Parametric flood insurance data.
- Privateer: Geospatial intelligence platform. ~$40M revenue, 85% gross margins.
- PHNX Materials: Critical minerals and metals. $5M DOE award.
- LGND: Geospatial data for AI. Google contract.
- Xage: Zero-trust cybersecurity. NVIDIA partnership.
- BioSqueeze: Wellbore sealing technology for oil & gas.

INDUSTRIAL TRANSFORMATION:
- Harbinger: EV mid-duty trucks. Pre-IPO, $300M revenue, Goldman Sachs engaged for IPO.
- Forum Mobility: Drayage trucking electrification. $75M Series B term sheet from CBRE.
- Kerrigan: AI robotic orchestration platform. Customers: Tesla, Rivian, Applied Materials.
- Molg: Robotics for AI data centers. $30M Series A at $92.5M post, 1.68x markup.
- Glacier: AI-enabled robotics recycling. Direct investment from major public company incoming.
- Alta: Domestic rare earth elements. 2.38x markup.
- Endolith: Bio-mining for critical minerals. $13.5M Series A led by Squadra Ventures.
- Graphyte: Carbon removal.
- ChemFinity: Industrial separation technology.
- Agrippa: Advanced maritime freight.
- Brightfield: AI platform for commercial batteries. $100M+ pipeline, acquisition interest from hyperscaler.
- Picarnot: Microgrid proposal and design automation. Co-investors: Sequoia, a16z Scouts.
- Radify Metals: Cold-plasma reactor for rare earth metals.

ACTIVELY RAISING NOW (highest priority touchpoints):
- Hexium: Series A Q2/Q3 2026
- Antora: Series C Q1 2026
- Bedrock Energy: ~$15M Series A
- Harbinger: $200M pre-IPO Series C extension

FUND II CONTEXT:
- Climate-focused pre-seed and seed
- 2.55x MOIC / 51% net IRR on Fund I
- First close completed on Fund II
- Portfolio companies serving 22 of top 50 global companies
"""

CLAUDE_SYSTEM_PROMPT = f"""{TACTICS_CONTEXT}

Given a Granola meeting note from an LP call, produce a SHORT Slack brief.

STEP 1: Check if the transcript has substantive content.
If it contains fewer than 5 real exchanges or only introductions with no investment discussion,
output only:
*INCOMPLETE NOTE* — [one sentence on what was captured]
*Action items*
1. Verify full notes were saved in Granola
[Add 1 to 2 obvious follow-ups if inferable from partial context]
Then stop. No bucket, no follow-up message.

STEP 2: For complete notes, mine the transcript and summary for:
- Sectors or themes the LP mentioned interest in
- Specific companies, deals, or investments they referenced
- Books, articles, or resources mentioned in conversation
- Objections or concerns raised
- Co-investment interest
- Any specific asks or commitments made

Then output exactly this format:

*[MALTBY BUCKET]* — [one sentence reason]

*Action items*
1. [specific, referenced from the notes]
2. [specific, referenced from the notes]
(3 to 4 max)

*Suggested follow-up*
[3 to 5 sentences. Write like a senior GP. Use the LP's name. Reference something specific
from the call. If they expressed interest in a sector, naturally weave in the most relevant
portfolio company from the list above — do not force it if there is no match. If a book or
resource was discussed, suggest sending it. End with one clear next step. No hyphens anywhere.
No AI sounding language. No generic openers. If STRUCTURAL NO, value add only, no pitch.]

Rules:
- Entire output must be concise enough for a Slack message
- No section headers beyond the three shown
- No cadence section
- No explanation of your reasoning beyond the bucket line
- Never use hyphens
"""


class FollowUpBot:
    def __init__(self):
        self.slack_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.affinity_key = os.getenv("AFFINITY_API_KEY", "").strip()
        self.affinity_list_name = os.getenv("AFFINITY_LIST_NAME", "Fundraising")
        self._slack_user_cache: Dict[str, str] = {}

    def _lookup_slack_user_id(self, email: str) -> Optional[str]:
        if email in self._slack_user_cache:
            return self._slack_user_cache[email]
        if not self.slack_token:
            return None
        try:
            r = requests.get(
                f"{SLACK_API_URL}/users.lookupByEmail",
                headers={"Authorization": f"Bearer {self.slack_token}"},
                params={"email": email},
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                user_id = data["user"]["id"]
                self._slack_user_cache[email] = user_id
                return user_id
            else:
                logger.warning(f"Could not find Slack user for {email}: {data.get('error')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Slack user lookup failed for {email}: {e}")
            return None

    def _post_to_slack(self, message: str) -> bool:
        if not self.slack_token:
            logger.warning("SLACK_BOT_TOKEN not set. Skipping post.")
            return False
        try:
            r = requests.post(
                f"{SLACK_API_URL}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.slack_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channel": FUNDRAISING_BOT_CHANNEL,
                    "text": message,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                logger.error(f"Slack post error: {data.get('error')}")
                return False
            logger.info("Slack message posted successfully")
            return True
        except requests.RequestException as e:
            logger.error(f"Slack post failed: {e}")
            return False

    def _get_lp_crm_data(self, lp_name: str) -> Dict:
        if not self.affinity_key:
            logger.info("AFFINITY_API_KEY not set. Skipping CRM lookup.")
            return {}
        try:
            client = AffinityClient(self.affinity_key)
            client.load_fundraising_list(self.affinity_list_name)
            match = client.find_match(lp_name)
            if match:
                logger.info(f"Affinity match for '{lp_name}': {match['name']} ({match['status']})")
                return match
            else:
                logger.info(f"No Affinity match found for '{lp_name}'")
                return {}
        except Exception as e:
            logger.error(f"Affinity lookup failed for '{lp_name}': {e}")
            return {}

    def _call_claude(self, user_prompt: str) -> Optional[str]:
        if not self.anthropic_key:
            logger.error("ANTHROPIC_API_KEY not set.")
            return None
        try:
            r = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 700,
                    "system": CLAUDE_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            content_blocks = data.get("content", [])
            text = " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            return text.strip() if text else None
        except requests.RequestException as e:
            logger.error(f"Claude API call failed: {e}")
            return None

    def process_note(self, note_context: Dict) -> bool:
        title = note_context.get("title", "Untitled meeting")
        owner_email = note_context.get("owner_email", "")
        owner_name = note_context.get("owner_name", "Unknown")
        summary = note_context.get("summary", "")
        transcript = note_context.get("transcript_text", "")
        created_at = note_context.get("created_at", "")

        logger.info(f"Processing note: '{title}' (owner: {owner_name})")

        lp_name = self._extract_lp_name(title)
        crm_data = self._get_lp_crm_data(lp_name) if lp_name else {}

        crm_context = ""
        if crm_data:
            crm_context = (
                f"\nCRM: {crm_data.get('name', 'N/A')} | "
                f"Status: {crm_data.get('status', 'Unknown')} | "
                f"Last activity: {crm_data.get('last_activity', 'Unknown')}"
            )

        user_prompt = (
            f"Meeting: {title}\n"
            f"Date: {created_at[:10] if created_at else 'unknown'}\n"
            f"GP: {owner_name}\n"
            f"{crm_context}\n\n"
            f"SUMMARY:\n{summary}\n\n"
            f"TRANSCRIPT:\n{transcript[:2500] if transcript else 'No transcript available.'}"
        )

        logger.info("Calling Claude for follow-up analysis...")
        analysis = self._call_claude(user_prompt)

        if not analysis:
            logger.error("Claude returned no response. Skipping Slack post.")
            return False

        slack_user_id = self._lookup_slack_user_id(owner_email)
        gp_tag = f"<@{slack_user_id}>" if slack_user_id else owner_name
        lp_display = lp_name or title
        date_display = created_at[:10] if created_at else "today"

        slack_message = (
            f"*{lp_display}* | {date_display} | {gp_tag}\n"
            f"{'─' * 40}\n"
            f"{analysis}"
        )

        return self._post_to_slack(slack_message)

    def _extract_lp_name(self, title: str) -> Optional[str]:
        title_clean = title.strip()

        for sep in [" <> ", " / ", " | ", " — ", " - "]:
            if sep in title_clean:
                parts = title_clean.split(sep, 1)
                for part in parts:
                    part = part.strip()
                    if "overture" not in part.lower():
                        return part
                return parts[0].strip()

        prefixes = [
            "call with ", "intro with ", "intro call with ",
            "meeting with ", "first meeting with ", "first meeting:",
            "catch up with ", "catch-up with ", "follow up with ",
            "diligence call with ", "lp call with ",
        ]
        title_lower = title_clean.lower()
        for prefix in prefixes:
            if title_lower.startswith(prefix):
                return title_clean[len(prefix):].strip().rstrip(":")

        if ":" in title_clean:
            candidate = title_clean.split(":")[0].strip()
            if len(candidate) > 3 and "overture" not in candidate.lower():
                return candidate

        return title_clean
