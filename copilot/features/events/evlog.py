"""Outbound event-log reader and an evidence whitelist applied before the client writes its line."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from copilot.core.disputes import SandboxClient
from .machine import digest, dispute_id, instant

EVENT_TYPES = ("CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED")
OVERLAP_SECONDS = 600  # overlapping watermark, failure-mode Design Change 2
PAGE_SIZE = 100
MAX_SPLITS = 12

LOG_FACTS = (
    "PayPal starts recording this event log when a webhook is registered for this app; a webhook registered with PayPal, "
    "pointing nowhere, is enough, and Proofie does not register one for you. Events already recorded stayed listed after "
    "the webhook was deleted (1.4 hours observed); PayPal does not state how long it keeps them, and its API does not say "
    "whether an event was delivered. Dispute CREATED and UPDATED events reached this log through an unreachable webhook "
    "and were still listed two minutes after its deletion in the sandbox. Retention beyond 1.4 hours was not measured."
)


def event_view(event):
    resource = event.get("resource")
    return {"id": event.get("id"), "event_type": event.get("event_type"), "create_time": event.get("create_time"),
            "resource_type": event.get("resource_type"), "resource_version": event.get("resource_version"),
            "event_version": event.get("event_version"),
            "link_rels": sorted({l["rel"] for l in event.get("links", []) if isinstance(l, dict) and isinstance(l.get("rel"), str)}),
            "resource": {"dispute_id": resource.get("dispute_id") if isinstance(resource, dict) else None}}


class EventLogClient(SandboxClient):
    """Evidence whitelist for list GETs. Instantiate only in the explicitly authorized real run."""
    def _log(self, record):
        if str(record.get("op", "")).startswith("evlog_events_list"):
            body = record.get("body")
            safe = {"count_field": body.get("count"), "events": [event_view(e) for e in body.get("events", []) if isinstance(e, dict)],
                    "page_link_rels": sorted({l["rel"] for l in body.get("links", []) if isinstance(l, dict) and isinstance(l.get("rel"), str)})} if isinstance(body, dict) and (record.get("status") or 0) < 400 else None
            # Drop any unanticipated response-bearing log extras, headers, signatures and exception text.
            record = {k: record[k] for k in ("op", "method", "path", "status", "ms", "debug_id", "transport_error", "outcome") if k in record}
            record["body"] = safe
        super()._log(record)


def metadata(event):
    did = (event.get("resource") or {}).get("dispute_id")
    eid = event.get("id")
    if not isinstance(eid, str) or not eid or len(eid) > 128 or not all(c.isalnum() or c in "-_" for c in eid):
        raise ValueError("invalid event ID")
    if event.get("event_type") not in EVENT_TYPES or not dispute_id(did):
        raise ValueError("event must reference a dispute and a supported event type")
    created = instant(event.get("create_time"))
    return {"event_id": eid, "event_type": event["event_type"], "create_time": created.isoformat() if created else None,
            "dispute_id": did, "sha256": digest(event)}


class IncompleteWindow(RuntimeError):
    pass


class Puller:
    def __init__(self, read_client, store, clock):
        # A plain real client would already have logged free text before a wrapper could scrub it.
        if isinstance(read_client.client, SandboxClient) and not isinstance(read_client.client, EventLogClient):
            raise ValueError("event log needs EventLogClient before evidence logging")
        self.client, self.store, self.clock = read_client, store, clock

    def _window(self, kind, start, end, depth=0):
        params = {"page_size": PAGE_SIZE, "start_time": start.isoformat(), "end_time": end.isoformat(), "event_type": kind}
        body = self.client.call("GET", "/v1/notifications/webhooks-events?" + urlencode(params), "evlog_events_list")
        events = body.get("events", [])
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            raise IncompleteWindow("event-list shape was not a list")
        next_page = any(isinstance(l, dict) and l.get("rel") == "next" for l in body.get("links", []))
        if len(events) >= PAGE_SIZE or next_page:
            if depth >= MAX_SPLITS or (end - start).total_seconds() <= 1:
                raise IncompleteWindow("full event window could not be subdivided safely")
            middle = start + (end - start) / 2
            return self._window(kind, start, middle, depth + 1) + self._window(kind, middle, end, depth + 1)
        return events

    def pull(self, consume):
        end = self.clock()
        last = instant(self.store.meta("log_end"))
        start = (last or end) - timedelta(seconds=OVERLAP_SECONDS)
        if start > end:
            raise IncompleteWindow("pull clock is before its watermark")
        self.store.set_meta("retention_coverage", "unknown")
        if last:
            self.store.set_meta("log_gap_seconds", max(0, int((end - last).total_seconds())))
        events = []
        for kind in EVENT_TYPES:
            events.extend(self._window(kind, start, end))
        pending = [metadata(event) for event in events]
        for event in pending:
            self.store.queue_event(event, "log")
        for event in pending:
            consume(event)  # trigger detail GET, never read resource state
        self.store.set_meta("log_end", end.isoformat())  # only all-filter/all-read success advances coverage
        return len(events)
