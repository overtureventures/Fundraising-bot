"""
run_followup_bot.py

Railway cron entry point for the LP follow-up bot.
Runs every 30 minutes during business hours Mon through Fri.
Polls Granola for new LP meeting notes, processes each one,
and posts the follow-up brief to #fundraising-bot on Slack.
"""

import os
import logging
from dotenv import load_dotenv
from granola_poller import GranolaPoller
from followup_bot import FollowUpBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()


def main():
    logger.info("=" * 60)
    logger.info("LP Follow-Up Bot: Starting run")
    logger.info("=" * 60)

    granola_key = os.getenv("GRANOLA_API_KEY", "").strip()
    if not granola_key:
        logger.error("GRANOLA_API_KEY not set. Exiting.")
        return

    poller = GranolaPoller(api_key=granola_key)
    lookback_hours = int(os.getenv("GRANOLA_LOOKBACK_HOURS", "1"))
    new_notes = poller.get_new_notes(lookback_hours=lookback_hours)

    if not new_notes:
        logger.info("No new LP notes found. Run complete.")
        return

    logger.info(f"Found {len(new_notes)} new LP note(s) to process")

    bot = FollowUpBot()
    processed = 0
    failed = 0

    for raw_note in new_notes:
        note_context = poller.extract_note_context(raw_note)
        title = note_context.get("title", "Untitled")

        logger.info(f"Processing: '{title}'")
        success = bot.process_note(note_context)

        if success:
            processed += 1
            logger.info(f"Posted brief for: '{title}'")
        else:
            failed += 1
            logger.warning(f"Failed to post brief for: '{title}'")

    logger.info("=" * 60)
    logger.info(f"Run complete. Processed: {processed} | Failed: {failed}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
