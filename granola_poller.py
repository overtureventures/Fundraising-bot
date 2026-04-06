"""
Granola Poller

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

# Keywords that indicate an LP meeting. Broad by design — better to over-include
# and let Claude filter than to miss a real LP call.
LP_TITLE_SIGNALS = [
    "lp", "investor", "limited partner", "family office", "endowment",
    "foundation", "pension", "fund of funds", "fof", "allocation",
    "capital call", "pitch", "fundraise", "fundraising", "close",
    "commitment", "diligence", "due diligence", "first meeting",
    "intro call", "catch up", "check in", "follow up", "follow-up",
    # Common meeting title patterns
    "intro", "meeting", "call", "sync", "connect",
    # Institution type signals
    "management company", "investment", "capital", "ventures", "partners",
    "asset management", "wealth", "trust", "university",
    # Known LP name fragments — add specific names here as needed
    "cameco", "wev", "moore", "ucea",
]

# Granola folder names that indicate LP fundraising content.
# Notes in these folders are always processed regardless of title.
LP_FOLDER_SIGNALS = [
    "fundraising", "lp", "investors", "fund ii", "fund 2",
    "limited partners", "capital raise",
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
        Return True if this note looks like an LP meeting.
        Checks title keywords first, then folder membership.
        """
        title = (note.get("title") or "").lower()

        # Title keyword match
        if any(signal in title for signal in LP_TITLE_SIGNALS):
            return True

        # Folder membership match
        folders = note.get("folders", []) or []
        for folder in folders:
            folder_name = (folder.get("name") or "").lower()
            if any(signal in folder_name for signal in LP_FOLDER_SIGNALS):
                logger.info(f"Note matched via folder '{folder_name}': {note.get('title')}")
                return True

        return False

    def get_new_notes(self, lookback_hours: int = 4) -> List[Dict]:
        """
        Fetch notes created or updated in the last lookback_hours window
        that haven't been processed yet. Returns fully enriched note dicts.
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
        """
        owner = note.get("owner", {})
        owner_email = owner.get("email", "")
        owner_name = owner.get("name", "")

        summary = note.get("summary", "")

        transcript_parts = []
        for segment in note.get("transcript", []):
            speaker_info = segment.get("speaker", {})
            source = speaker_info.get("source", "unknown")
            text = segment.get("text", "").strip()
            if text:
                label = "GP" if source == "microphone" else "LP"
                transcript_parts.append(f"{label}: {text}")

        transcript_text = "\n".join(transcript_parts)

        folders = note.get("folders", []) or []
        folder_names = [f.get("name", "") for f in folders if f.get("name")]

        return {
            "note_id": note.get("id"),
            "title": note.get("title", "Untitled meeting"),
            "owner_email": owner_email,
            "owner_name": owner_name,
            "summary": summary,
            "transcript_text": transcript_text,
            "created_at": note.get("created_at", ""),
            "folders": folder_names,
        }
