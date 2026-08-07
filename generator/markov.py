"""
A tiny word-level Markov chain generator.

Deliberately unsophisticated: it builds a chain from whatever sentences
it's handed (usually a channel or server corpus) and walks it randomly.
No smoothing, no weighting beyond natural frequency, no cleverness.
"""
import random


def tokenize(text: str) -> list[str]:
    return text.split()


class MarkovGenerator:
    def __init__(self, order: int = 2):
        self.order = max(1, order)

    def _build(self, sentences: list[str]):
        chain: dict[tuple, list] = {}
        starts: list[tuple] = []
        order = self.order

        for sentence in sentences:
            words = tokenize(sentence)
            if len(words) < order + 1:
                continue
            starts.append(tuple(words[:order]))
            for i in range(len(words) - order):
                key = tuple(words[i:i + order])
                nxt = words[i + order]
                chain.setdefault(key, []).append(nxt)
            # mark a valid stopping point at the end of the sentence
            end_key = tuple(words[-order:])
            chain.setdefault(end_key, []).append(None)

        return chain, starts

    def generate(self, sentences: list[str], min_words: int = 4,
                 max_words: int = 35, attempts: int = 15) -> str | None:
        chain, starts = self._build(sentences)
        if not starts:
            return None

        best = None
        for _ in range(attempts):
            words = list(random.choice(starts))
            for _ in range(max_words - len(words)):
                options = chain.get(tuple(words[-self.order:]))
                if not options:
                    break
                nxt = random.choice(options)
                if nxt is None:
                    break
                words.append(nxt)

            if min_words <= len(words) <= max_words:
                return " ".join(words)
            if best is None or len(words) > len(best):
                best = words

        if best and len(best) >= min_words:
            return " ".join(best[:max_words])
        return None
