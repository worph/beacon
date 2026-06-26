"""User-supplied annotations for what Beacon tells the LLM, persisted to disk.

Three kinds, all optional and empty by default:

  - instructions_note : global text added to the top-level `instructions`,
                        just before the "Available servers:" list.
  - descriptions      : per-server short description that *replaces* the
                        discovered/external one (shown everywhere).
  - server_notes      : per-server long text *added* to `server_doc` output
                        (drill-down only — kept out of the compact overview).

Discovered servers are fully replaced on every discovery cycle and external
servers are rebuilt on every poll, so none of this can live on the
RegisteredServer dataclass — it would be wiped within one interval. Instead we
keep it in a side store keyed by server name and re-apply it at read time.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/app/data/annotations.json"


def _config_path() -> Path:
    return Path(os.environ.get("ANNOTATIONS_CONFIG_PATH", DEFAULT_CONFIG_PATH))


class AnnotationStore:
    """Stores the global instructions note plus per-server overrides and notes."""

    def __init__(self) -> None:
        self._instructions_note: str = ""
        self._overrides: dict[str, str] = {}  # name -> description override
        self._notes: dict[str, str] = {}      # name -> server_doc note

    def load(self) -> None:
        path = _config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load annotations at %s: %s", path, e)
            return
        if not isinstance(data, dict):
            return
        note = data.get("instructions_note")
        if isinstance(note, str):
            self._instructions_note = note
        self._overrides = self._clean_map(data.get("descriptions"))
        self._notes = self._clean_map(data.get("server_notes"))

    @staticmethod
    def _clean_map(raw) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}

    def _save(self) -> None:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "instructions_note": self._instructions_note,
            "descriptions": self._overrides,
            "server_notes": self._notes,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    # --- global instructions note ------------------------------------------

    def get_instructions_note(self) -> str:
        return self._instructions_note

    def set_instructions_note(self, text: str) -> bool:
        """Set the global note. Empty/whitespace clears it. Returns True if set."""
        new = text if text and text.strip() else ""
        changed = new != self._instructions_note
        self._instructions_note = new
        if changed:
            self._save()
        return bool(new)

    # --- per-server description override ------------------------------------

    def get(self, name: str) -> str | None:
        return self._overrides.get(name)

    def all(self) -> dict[str, str]:
        return dict(self._overrides)

    def set(self, name: str, description: str) -> bool:
        """Set a description override. Empty/whitespace clears it (restore default).

        Returns True if an override is now in effect, False if it was cleared.
        """
        if description and description.strip():
            self._overrides[name] = description
            self._save()
            return True
        cleared = self._overrides.pop(name, None) is not None
        if cleared:
            self._save()
        return False

    # --- per-server server_doc note ----------------------------------------

    def get_note(self, name: str) -> str | None:
        return self._notes.get(name)

    def all_notes(self) -> dict[str, str]:
        return dict(self._notes)

    def set_note(self, name: str, note: str) -> bool:
        """Set a server_doc note. Empty/whitespace clears it. Returns True if set."""
        if note and note.strip():
            self._notes[name] = note
            self._save()
            return True
        cleared = self._notes.pop(name, None) is not None
        if cleared:
            self._save()
        return False

    # --- removal -----------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Drop all customization for a server. Returns True if anything existed."""
        had_override = self._overrides.pop(name, None) is not None
        had_note = self._notes.pop(name, None) is not None
        existed = had_override or had_note
        if existed:
            self._save()
        return existed
