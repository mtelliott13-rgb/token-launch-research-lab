"""Theme tags: one of a handful of themes per token, from its name and description.

The rules are plain keyword lists in config/themes.yaml, tried in the order they are written;
the first theme with a match wins and a token that matches nothing is "other". This is a
rough sort, not a judgement: a keyword cannot tell that "Jordan" is a person and not a
country. Check the samples on the Patterns page and adjust the lists.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from radar.config import ROOT

OTHER = "other"
DEFAULT_PATH = ROOT / "config" / "themes.yaml"

_CAMEL = re.compile(r"([a-z])([A-Z])")
_SEPARATORS = re.compile(r"[^a-z]+")


def _normalise(*parts: str | None) -> str:
    """Lower-case words separated by single spaces, with a space at each end: " fat cat "."""
    text = " ".join(_CAMEL.sub(r"\1 \2", p) for p in parts if p)
    return f" {_SEPARATORS.sub(' ', text.lower()).strip()} "


class Tagger:
    def __init__(self, rules: Mapping[str, Mapping[str, Any]], version: str = ""):
        self.version = version
        self.themes = [*rules, OTHER]
        self._rules = [
            (
                theme,
                tuple(f" {_normalise(str(w)).strip()} " for w in rule.get("words") or ()),
                tuple(str(w).lower() for w in rule.get("contains") or ()),
            )
            for theme, rule in rules.items()
        ]

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_PATH) -> "Tagger":
        raw = Path(path).read_bytes()
        return cls(yaml.safe_load(raw) or {}, hashlib.sha256(raw).hexdigest()[:16])

    def tag(self, name: str | None, description: str | None = None) -> str:
        spaced = _normalise(name, description)
        squashed = spaced.replace(" ", "")
        for theme, words, contains in self._rules:
            if any(w in spaced for w in words) or any(c in squashed for c in contains):
                return theme
        return OTHER
