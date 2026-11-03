"""Explicit USD/token rates; unknown model IDs fail closed."""

RATES = {
    "gpt-5-nano": (0.05 / 1_000_000, 0.40 / 1_000_000),
    "gpt-5.6-luna": (0.20 / 1_000_000, 1.20 / 1_000_000),
    "gpt-6.1-sol": (2.00 / 1_000_000, 10.00 / 1_000_000),
    "gpt-6-luna": (0.10 / 1_000_000, 0.50 / 1_000_000),
    "gpt-6.1-terra": (1.00 / 1_000_000, 5.00 / 1_000_000),
}
EVALUATION_MODELS = frozenset({"gpt-5-nano", "gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-terra"})
