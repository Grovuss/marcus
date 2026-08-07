"""
Fragment recombination: chop logged messages into a "head" and "tail"
around a natural-ish split point, then stitch the head of one message
to the tail of a completely different one. Tends to preserve more
recognizable phrasing than pure Markov generation.
"""
import random

# Words that make a reasonably natural split point ("...on fire" | "again")
SPLIT_WORDS = {
    "and", "but", "because", "so", "then", "when", "while", "if", "that",
    "who", "which", "in", "on", "at", "with", "for", "the", "to", "of",
    "is", "was", "are", "were",
}


def tokenize(text: str) -> list[str]:
    return text.split()


class FragmentGenerator:
    def _split_index(self, words: list[str]) -> int:
        candidates = [
            i for i, w in enumerate(words)
            if w.lower() in SPLIT_WORDS and 1 <= i <= len(words) - 2
        ]
        if candidates:
            return random.choice(candidates)
        # fall back to a roughly-middle split so both halves have content
        lo = max(1, len(words) // 3)
        hi = max(lo + 1, (len(words) * 2) // 3)
        hi = min(hi, len(words) - 1)
        if lo >= hi:
            return max(1, len(words) // 2)
        return random.randint(lo, hi)

    def _build_fragments(self, sentences: list[str]):
        heads, tails = [], []
        for sentence in sentences:
            words = tokenize(sentence)
            if len(words) < 4:
                continue
            idx = self._split_index(words)
            heads.append(words[:idx])
            tails.append(words[idx:])
        return heads, tails

    def generate(self, sentences: list[str], min_words: int = 4,
                 max_words: int = 35, attempts: int = 15) -> str | None:
        heads, tails = self._build_fragments(sentences)
        if not heads or not tails:
            return None

        best = None
        for _ in range(attempts):
            head = random.choice(heads)
            tail = random.choice(tails)
            combined = head + tail
            if min_words <= len(combined) <= max_words:
                return " ".join(combined)
            if best is None or abs(len(combined) - min_words) < abs(len(best) - min_words):
                best = combined

        if best:
            trimmed = best[:max_words]
            if len(trimmed) >= min_words:
                return " ".join(trimmed)
        return None
