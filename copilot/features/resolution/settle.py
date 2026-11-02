"""Wait for PayPal to finish updating a dispute after a seller write.

Observed sandbox behaviour: after ANY seller write the dispute shows UNDER_REVIEW with only
`self` for about 15 to 60 s; a second write inside that window answers 422. The default is 8 reads 10 s apart (80 s),
longer than the observed window. Reads only, through the injected client; a PayPal failure is a Limitation, never an
exception out of the feature; programming errors still raise."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


from copilot.core import paypal_words
from copilot.core.context import SRC_DISPUTE, Limitation
from copilot.core.disputes import normalize
from copilot.features.resolution.order import is_settling


@dataclass(frozen=True)
class SettleResult:
    settled: bool
    norm: dict | None
    reads: int
    limitations: tuple = ()


def failure_message(exc: Exception, what: str) -> str | None:
    """Plain-English text for an expected PayPal failure; None for a programming error."""
    return paypal_words.could_not(exc, what)


def wait_until_settled(client, dispute_id: str, *, polls: int = 8, delay_seconds: int = 10, sleep=time.sleep,
                       op: str = "resolution_poll", log_extra: dict | None = None) -> SettleResult:
    norm, reads = None, 0
    for attempt in range(polls):
        if attempt:
            sleep(delay_seconds)
        try:
            raw = client.call("GET", f"/v1/customer/disputes/{dispute_id}", op,
                              **({"log_extra": log_extra} if log_extra is not None else {}))
        except Exception as exc:  # noqa: BLE001 - classified; programming errors re-raised
            msg = failure_message(exc, "read the dispute")
            if msg is None:
                raise
            return SettleResult(False, norm, reads + 1, (Limitation(SRC_DISPUTE, msg, paypal_words.technical_of(exc)),))
        reads += 1
        norm = normalize(raw, datetime.now(timezone.utc))
        if not is_settling(norm):
            return SettleResult(True, norm, reads)
    return SettleResult(False, norm, reads, (Limitation(
        SRC_DISPUTE, f"PayPal is still updating this dispute after {polls * delay_seconds} seconds; refresh later"),))
