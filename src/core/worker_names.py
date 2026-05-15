"""Generate Docker-style random names (adjective_noun) for swarm workers.

Provides deterministic name generation using stdlib random, suitable for
identifying workers in a swarm pool without external dependencies.
"""

import random

ADJECTIVES = [
    "bold", "calm", "eager", "swift", "quiet", "bright", "gentle", "solid",
    "brave", "keen", "warm", "cool", "sharp", "steady", "fleet", "clear",
    "crisp", "deep", "fair", "firm", "free", "grand", "pure", "rich", "ripe",
    "safe", "smooth", "soft", "still", "strong", "sure", "tidy", "trim",
    "true", "vast", "light", "wise", "agile", "apt", "ardent", "austere",
    "blithe", "brief", "brisk", "buoyant", "candid", "chill", "dapper",
    "deft", "dense", "dim", "dulcet",
]

NOUNS = [
    "payne", "noether", "lovelace", "turing", "ada", "hopper", "spark",
    "ridge", "pine", "creek", "grove", "fern", "moss", "stone", "cove",
    "bloom", "cliff", "dune", "fjord", "glen", "hawk", "iris", "jay",
    "kite", "lake", "lark", "leaf", "lynx", "mist", "moon", "peak",
    "plum", "pond", "quail", "rain", "reed", "rill", "root", "sage",
    "sand", "shore", "silk", "snow", "star", "stem", "tide", "vale",
    "wave", "wick", "wren",
]


def generate_worker_name(rng: random.Random | None = None) -> str:
    """Return a random ``adjective_noun`` name.

    Args:
        rng: Optional ``random.Random`` instance for deterministic output.
             Defaults to the module-level ``random``.

    Returns:
        A string like ``bold_payne``.
    """
    r = rng or random
    adjective = r.choice(ADJECTIVES)
    noun = r.choice(NOUNS)
    return f"{adjective}_{noun}"

