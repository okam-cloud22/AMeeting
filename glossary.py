"""
glossary.py — post-transcription find/fix pass driven by an editable glossary.json.

glossary.json format (edit freely, no code changes needed):

{
  "replacements": [
    {"wrong": ["misheard variant 1", "variant 2"], "right": "Canonical Term"},
    {"wrong": "single variant",                    "right": "Another Term"}
  ]
}

Matching is case-insensitive and whole-word (works for Greek and Latin text).
Longer "wrong" strings are applied first so multi-word phrases win over
their sub-words.
"""

import json
import re
from pathlib import Path


def load_glossary(path):
    """Return list of (wrong, right) pairs, longest wrong first."""
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    pairs = []
    for item in data.get("replacements", []):
        right = str(item.get("right", "")).strip()
        wrongs = item.get("wrong", [])
        if isinstance(wrongs, str):
            wrongs = [wrongs]
        if not right:
            continue
        for w in wrongs:
            w = str(w).strip()
            if w and w.lower() != right.lower():
                pairs.append((w, right))
            elif w:
                # identical except for case -> still useful as a case normalizer
                pairs.append((w, right))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def apply_glossary(text, pairs):
    """Apply all replacements. Returns (corrected_text, replacement_count)."""
    total = 0
    for wrong, right in pairs:
        pattern = re.compile(
            r"(?<!\w)" + re.escape(wrong) + r"(?!\w)",
            re.IGNORECASE | re.UNICODE,
        )
        text, n = pattern.subn(right, text)
        total += n
    return text, total


def correct_file(raw_path, glossary_path):
    """Read raw transcript, apply glossary, write *_corrected.txt next to it.

    Returns (corrected_path, replacement_count).
    """
    raw_path = Path(raw_path)
    pairs = load_glossary(glossary_path)
    text = raw_path.read_text(encoding="utf-8-sig")
    corrected, count = apply_glossary(text, pairs)

    stem = raw_path.stem
    if stem.endswith("_transcript"):
        out_name = stem + "_corrected.txt"
    else:
        out_name = stem + "_corrected.txt"
    out_path = raw_path.with_name(out_name)
    out_path.write_text(corrected, encoding="utf-8")
    return out_path, count
