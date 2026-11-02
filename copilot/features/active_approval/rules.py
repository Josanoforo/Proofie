"""Human review cautions, separate from Seller Protection eligibility."""
import re
import unicodedata
from dataclasses import dataclass

PHRASE = "I REVIEWED EACH CLAIM"
SOURCE = "PayPal Seller Protection MX (2025-01-13), p.3 transaction values; app review caution uses >="


@dataclass(frozen=True)
class Threshold:
    currency: str
    minor: int
    units: int
    source: str

    @property
    def display(self):
        scale = 10 ** self.units
        return f"{self.minor // scale}.{self.minor % scale:0{self.units}d} {self.currency}"


THRESHOLDS = (Threshold("MXN", 1000000, 2, SOURCE), Threshold("USD", 75000, 2, SOURCE))
FRAUD_WORDS = ("fraud", "fraudulent", "scam", "scammer", "dishonest", "fraude", "fraudulento",
               "fraudulenta", "estafa", "estafador", "estafadora", "deshonesto", "deshonesta")


def typed_reasons(amount, currency, texts) -> tuple[str, ...]:
    thresholds = {t.currency: t for t in THRESHOLDS}
    threshold = thresholds.get(currency)
    reasons = []
    if threshold is None or type(amount) is not int or amount < 0:
        reasons.append("Amount or currency has no known review threshold; confirm by typing.")
    elif amount >= threshold.minor:
        reasons.append(f"Amount is at or above the review threshold: {threshold.display}.")
    words = set(re.findall(r"\w+", "".join(c for c in unicodedata.normalize(
        "NFKD", " ".join(texts).casefold()) if not unicodedata.combining(c))))
    found = sorted(words.intersection(FRAUD_WORDS))
    if found:
        reasons.append("Your wording includes a fraud caution word: " + ", ".join(found) + ".")
    return tuple(reasons)
