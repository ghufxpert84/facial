"""Parses 'Field Report' captions/text into structured fields.

PLACEHOLDER PARSING RULES — this is a generic "Key: Value" line parser that
triggers on the phrase "field report" appearing anywhere in the text. Once
you share a real anonymized sample message (see plan's open items), tighten
this to match the actual format your workers use.
"""
import re

MARKER_RE = re.compile(r"field\s*report", re.IGNORECASE)


def extract_field_report(text):
    """Returns a dict of parsed fields if text looks like a field report,
    otherwise None. The dict may be empty (no recognized key:value lines)
    even when a report is detected — raw_text is always kept as the source
    of truth alongside it."""
    if not text or not MARKER_RE.search(text):
        return None

    parsed = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            parsed[key] = value
    return parsed
