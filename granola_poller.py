"""
granola_poller.py

Polls the Granola Enterprise API for new meeting notes since the last run.
Uses a local state file to track which notes have already been processed.
Returns a list of new note dicts ready for the follow-up bot to consume.
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

GRANOLA_API_BASE = "https://public-api.granola.ai/v1"
STATE_FILE = Path("/tmp/granola_seen_notes.json")

# Keywords that indicate an LP meeting vs an internal call
LP_TITLE_SIGNALS = [
    "lp", "investor", "limited partner", "family office", "endowment",
    "foundation", "pension", "fund of funds", "fof", "allocation",
    "pitch", "fundraise", "fundraising", "close", "commitment",
    "diligence", "due diligence", "first meeting", "intro call",
    "catch up", "check in", "follow up"
]


class GranolaPoller:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        try:
            url = f"{GRANOLA_API_BASE}{endpoint}"
            r = requests.get(url, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"Granola API error on {endpoint}: {e}")
            return None

    def _load_seen_ids(self) -> set:
        if STATE_FILE.exists():
            try:
                return set(json.loads(STATE_FILE.read_text()))
            except Exception:
                return set()
        return set()

    def _save_seen_ids(self, ids: set):
        try:
            STATE_FILE.write_text(json.dumps(list(ids)))
        except Exception as e:
            logger.warning(f"Could not save state file: {e}")

    def _is_lp_meeting(self, note: Dict) -> bool:
        """
        Heuristic filter: only process notes that look like LP meetings.
        Checks the title for LP-related keywords. When in doubt, include it
        so the Claude prompt can make the final call.
        """
        title = (note.get("title") or "").lower()
        return any(signal in title for signal in LP_TITLE_SIGNALS)

    def get_new_notes(self, lookback_hours: int = 1) -> List[Dict]:
        """
        Fetch notes created in the last lookback_hours window that have not
        been processed yet. Returns fully enriched note dicts.
        """
        seen_ids = self._load_seen_ids()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(f"Polling Granola for notes since {cutoff_str}")

        all_notes = []
        cursor = None

        while True:
            params = {"created_after": cutoff_str}
            if cursor:
                params["cursor"] = cursor

            result = self._get("/notes", params=params)
            if not result:
                break

            batch = result.get("notes", [])
            all_notes.extend(batch)

            if not result.get("hasMore"):
                break
            cursor = result.get("cursor")

        logger.info(f"Found {len(all_notes)} total notes in window")

        new_notes = []
        new_ids = set()

        for note in all_notes:
            note_id = note.get("id")
            if not note_id or note_id in seen_ids:
                continue

            if not self._is_lp_meeting(note):
                logger.info(f"Skipping non-LP note: {note.get('title', 'untitled')}")
                new_ids.add(note_id)
                continue

            # Fetch full note with transcript
            full_note = self._get(f"/notes/{note_id}?include=transcript")
            if full_note:
                transcript_segments = full_note.get("transcript", [])
                if not transcript_segments:
                    logger.warning(
                        f"Note '{note.get('title')}' has no transcript. "
                        "Check Granola Settings > Workspace > API transcript access."
                    )
                new_notes.append(full_note)
                new_ids.add(note_id)
                logger.info(f"New LP note queued: {full_note.get('title', 'untitled')}")
            else:
                logger.warning(f"Could not fetch full note for {note_id}")

        self._save_seen_ids(seen_ids | new_ids)

        logger.info(f"Returning {len(new_notes)} new LP notes for processing")
        return new_notes

    def extract_note_context(self, note: Dict) -> Dict:
        """
        Pull the fields the follow-up bot needs from a raw Granola note.
        Returns a clean dict with: title, owner_email, owner_name,
        summary, transcript_text, created_at.
        """
        owner = note.get("owner", {})
        owner_email = owner.get("email", "")
        owner_name = owner.get("name", "")

        summary = note.get("summary", "")

        # Flatten transcript into readable GP / LP labelled lines
        transcript_parts = []
        for segment in note.get("transcript", []):
            speaker_info = segment.get("speaker", {})
            source = speaker_info.get("source", "unknown")
            text = segment.get("text", "").strip()
            if text:
                label = "GP" if source == "microphone" else "LP"
                transcript_parts.append(f"{label}: {text}")

        transcript_text = "\n".join(transcript_parts)

        return {
            "note_id": note.get("id"),
            "title": note.get("title", "Untitled meeting"),
            "owner_email": owner_email,
            "owner_name": owner_name,
            "summary": summary,
            "transcript_text": transcript_text,
            "created_at": note.get("created_at", ""),
        }
