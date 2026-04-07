"""
followup_bot.py

Takes a processed Granola note context dict, looks up the LP in Affinity,
calls Claude with the full fundraising tactics prompt, and posts the result
to #fundraising-bot on Slack, tagging the GP who had the call.
"""

import os
import re
import logging
import requests
from typing import Dict, Optional
from affinity import AffinityClient

logger = logging.getLogger(__name__)

FUNDRAISING_BOT_CHANNEL = "C0AQHP58A0Z"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
SLACK_API_URL = "https://slack.com/api"

CLAUDE_SYSTEM_PROMPT = """
You are the fundraising intelligence system for Overture Ventures, a climate-focused
early-stage venture fund currently raising Fund II.

MALTBY BUCKET CLASSIFICATION:
- REAL SHOT: meeting notes contain signals like "fits our mandate", "next steps",
  "send materials", "interested", or a clear commitment to a follow-on step
- NEEDS WORK: notes contain "interesting", "still getting our arms around", "TBD",
  or there was no clear next step agreed on the call
- STRUCTURAL NO: notes contain "already in X funds", "capacity issue", "timing",
  "I like you but...", or any clear structural reason they cannot invest now

FOLLOW-UP ACTION BY BUCKET:

REAL SHOT: Send a deal update or fund momentum email. Propose a specific next step
(IC materials, reference call, data room link). Tone is confident and responsive.
Cadence: every 2 to 3 days.

NEEDS WORK: Send a "show don't tell" email. Frame it as "You mentioned X, here is
a specific proof point." Conclude with a proposed next meeting. Cadence: 2 to 3 weeks.

STRUCTURAL NO: Send a no-ask value-add only. No pitch. No fund reference. Goal is
warmth for the next fund cycle. Cadence: quarterly.

LP TYPE ADJUSTMENTS:
- Institutional LP: formal written communication, IC-ready materials, never pressure
- Family office or HNWI: personal and direct, text or personal email preferred,
  co-invest access is high value
- Fund of funds: deep transparency, strong references, proprietary sourcing proof

WARM-UP RULE: Never make a capital ask without 2 to 3 prior value-add touchpoints.
If this looks like a first or second touch, flag it and recommend warming up first.

RED FLAGS TO CALL OUT IF PRESENT:
- GP committed to a next step on the call but no action item captured
- LP gave a soft no but GP is treating it as a maybe
- More than 7 days have passed since the call with no follow-up sent

FORMAT YOUR RESPONSE WITH THESE EXACT SECTION HEADERS (no markdown except bold headers):

MALTBY BUCKET
[single line: Real Shot / Needs Work / Structural No, with one sentence rationale]

ACTION ITEMS
[numbered list of specific things the GP owes, tied to what was said on the call]

SUGGESTED FOLLOW-UP MESSAGE
[draft message ready to send, personalized to this LP, no hyphens, no AI sounding language]

FOLLOW-UP CADENCE
[one line: recommended next contact window based on bucket and LP type]
"""


class FollowUpBot:
    def __init__(self):
        self.slack_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.affinity_key = os.getenv("AFFINITY_API_KEY", "").strip()
        self.affinity_list_name = os.getenv("AFFINITY_LIST_NAME", "Fundraising")
        self._slack_user_cache: Dict[str, str] = {}

    # ── Slack helpers ────────────────────────────────────────────────────────

    def _lookup_slack_user_id(self, email: str) -> Optional[str]:
        """Resolve a Granola owner email to a Slack user ID for tagging."""
        if not email:
            return None
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
                logger.warning(f"Slack user lookup failed for {email}: {data.get('error')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Slack user lookup error for {email}: {e}")
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

    # ── Affinity lookup ──────────────────────────────────────────────────────

    def _get_lp_crm_data(self, lp_name: str) -> Dict:
        if not self.affinity_key:
            logger.info("AFFINITY_API_KEY not set. Skipping CRM lookup.")
            return {}

        try:
            client = AffinityClient(self.affinity_key)
            client.load_fundraising_list(self.affinity_list_name)
            match = client.find_match(lp_name)
            if match:
                logger.info(f"Affinity match for '{lp_name}': {match.get('name')} ({match.get('status')})")
                return match
            else:
                logger.info(f"No Affinity match found for '{lp_name}'")
                return {}
        except Exception as e:
            logger.error(f"Affinity lookup failed for '{lp_name}': {e}")
            return {}

    # ── Claude call ──────────────────────────────────────────────────────────

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
                    "max_tokens": 1500,
                    "system": CLAUDE_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            content_blocks = data.get("content", [])
            text = " ".join(
                b.get("text", "") for b in content_blocks if b.get("type") == "text"
            )
            return text.strip() if text else None
        except requests.RequestException as e:
            logger.error(f"Claude API call failed: {e}")
            return None

    # ── LP name extraction ───────────────────────────────────────────────────

    def _extract_lp_name(self, title: str) -> Optional[str]:
        """
        Parse meeting title to extract the LP or organization name.
        Handles common formats:
          "BlackRock <> Overture"
          "Call with Wellcome Trust"
          "Overture / Stanford Endowment"
          "First meeting: Harvard Management Company"
        """
        title_clean = title.strip()

        for sep in [" <> ", " / ", " | ", " -- ", " - "]:
            if sep in title_clean:
                parts = title_clean.split(sep, 1)
                for part in parts:
                    part = part.strip()
                    if "overture" not in part.lower():
                        return part
                return parts[0].strip()

        prefixes = [
            "call with ", "intro with ", "intro call with ",
            "meeting with ", "first meeting with ", "first meeting: ",
            "catch up with ", "follow up with ",
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

        logger.info(f"Could not parse LP name from title '{title}'. Using full title for CRM lookup.")
        return title_clean

    # ── Main entry point ─────────────────────────────────────────────────────

    def process_note(self, note_context: Dict) -> bool:
        """
        Full pipeline for one note:
        1. Extract LP name from meeting title
        2. Look up LP in Affinity
        3. Call Claude with all context
        4. Post to Slack tagging the GP
        """
        title = note_context.get("title", "Untitled meeting")
        owner_email = note_context.get("owner_email", "")
        owner_name = note_context.get("owner_name", "Unknown")
        summary = note_context.get("summary", "")
        transcript = note_context.get("transcript_text", "")
        created_at = note_context.get("created_at", "")

        logger.info(f"Processing note: '{title}' (owner: {owner_name})")

        if not summary and not transcript:
            logger.warning(
                f"Note '{title}' has no summary or transcript. "
                "Enable transcript access in Granola Settings > Workspace. Skipping."
            )
            return False

        lp_name = self._extract_lp_name(title)
        crm_data = self._get_lp_crm_data(lp_name) if lp_name else {}

        crm_context = ""
        if crm_data:
            crm_context = f"""
CRM DATA FOR THIS LP:
- Organization: {crm_data.get('name', 'N/A')}
- Pipeline status: {crm_data.get('status', 'Unknown')}
- Last activity: {crm_data.get('last_activity', 'Unknown')}
- Notes on file: {crm_data.get('notes', 'None')}
"""

        user_prompt = f"""
Meeting title: {title}
Date: {created_at}
GP on the call: {owner_name} ({owner_email})

MEETING SUMMARY FROM GRANOLA:
{summary if summary else "No summary available."}

TRANSCRIPT:
{transcript[:3000] if transcript else "No transcript available."}

{crm_context}
LP NAME IDENTIFIED: {lp_name or "Could not identify from title"}

Based on the above, provide the four sections in your standard format:
MALTBY BUCKET, ACTION ITEMS, SUGGESTED FOLLOW-UP MESSAGE, FOLLOW-UP CADENCE.
"""

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
            f"*Follow-Up Brief: {lp_display}* | {date_display}\n"
            f"Call owner: {gp_tag}\n"
            f"{'=' * 48}\n"
            f"{analysis}"
        )

        return self._post_to_slack(slack_message)
