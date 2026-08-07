"""
Wraps the Markov and fragment generators behind one interface. Marcus
picks a method randomly ("mixed" mode) or can be pinned to one method
per-guild via /marcus config in the DB (generation_mode).
"""
import random

from .markov import MarkovGenerator
from .fragments import FragmentGenerator

MAX_RESPONSE_CHARS = 300


class ResponseGenerator:
    def __init__(self, order: int = 2):
        self.markov = MarkovGenerator(order=order)
        self.fragments = FragmentGenerator()

    def generate(self, sentences: list[str], mode: str = "mixed",
                 min_words: int = 4, max_words: int = 35) -> str | None:
        if not sentences:
            return None

        if mode == "markov":
            method = "markov"
        elif mode == "fragments":
            method = "fragments"
        else:
            method = random.choice(["markov", "fragments"])

        text = None
        if method == "markov":
            text = self.markov.generate(sentences, min_words, max_words)
            if not text:
                text = self.fragments.generate(sentences, min_words, max_words)
        else:
            text = self.fragments.generate(sentences, min_words, max_words)
            if not text:
                text = self.markov.generate(sentences, min_words, max_words)

        if not text:
            return None

        text = " ".join(text.split())
        if len(text) > MAX_RESPONSE_CHARS:
            text = text[:MAX_RESPONSE_CHARS].rsplit(" ", 1)[0]
        return text or None
