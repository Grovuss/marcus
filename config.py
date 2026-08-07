"""
Loads config.yaml. These values are only used as the initial defaults
when a guild is seen for the first time (seeded into SQLite) and as a
fallback if a guild row is somehow missing. Live configuration changes
made via slash commands always live in SQLite, not this file.
"""
import os
import yaml

DEFAULTS = {
    "bot": {"name": "Marcus"},
    "response": {
        "global_chance": 3,
        "cooldown_seconds": 30,
        "max_words": 35,
        "min_words": 4,
    },
    "gif": {
        "enabled": True,
        "response_chance": 25,
        "channel_local_preference": True,
    },
    "generation": {
        "mode": "mixed",
        "markov_order": 2,
    },
    "database": {"path": "marcus.db"},
}


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str = "config.yaml") -> dict:
    cfg = DEFAULTS
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        cfg = _merge(DEFAULTS, loaded)
    return cfg


CONFIG = load_config()
