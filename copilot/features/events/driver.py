"""Three GET-only producers; single-flight leases and replay of incomplete work."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .evlog import Puller, EVENT_TYPES
from .machine import snapshot, eligible, dispute_id, instant
from .rate import ReadClient


class Driver:
    def __init__(self, store, client, budget=None, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.store, self.read = store, ReadClient(client, budget)
        self.clock, self.monotonic = clock, monotonic

    def get(self, did, source, event=None, receiver_row=None):
        if not dispute_id(did):
            raise ValueError("invalid dispute ID")
        with self.store.lease("get:" + did, self.clock().timestamp()):
            body = self.read.call("GET", "/v1/customer/disputes/" + did, "events_detail_get")
            observed = snapshot(body, source, self.clock(), did)
            return self.store.commit_get(observed, event, receiver_row)

    def event(self, event, source, receiver_row=None):
        self.store.count("seen_" + source)
        created = instant(event.get("create_time"))
        if created is not None:
            self.store.set_meta("last_lag_ms_" + source, max(0, round((self.clock() - created).total_seconds() * 1000)))
        if self.store.has_event(event["event_id"]):
            self.store.count("duplicate_events")
            return "duplicate_event"
        self.store.queue_event(event, source, receiver_row)
        return self.get(event["dispute_id"], source, event, receiver_row)

    def retry_pending(self, source=None):
        count = 0
        for event, origin, row in self.store.pending(source):
            self.get(event["dispute_id"], origin, event, row)
            count += 1
        return count

    def receiver(self):
        processed = 0
        for row in self.store.receiver_rows():
            if row["event_type"] not in EVENT_TYPES or not dispute_id(row["dispute_id"]):
                self.store.count("rejected_receiver")
                continue
            event = {"event_id": row["event_id"], "event_type": row["event_type"], "dispute_id": row["dispute_id"],
                     "create_time": None, "sha256": row["body_sha256"]}
            self.event(event, "webhook", row["id"])
            processed += 1
        return processed

    def sweep(self):
        start, processed = self.monotonic(), 0
        with self.store.lease("sweep", self.clock().timestamp(), seconds=1800):
            try:
                for row in eligible(self.store.snapshots(), self.clock()):
                    self.get(row.dispute_id, "sweep")
                    self.store.count("sweep_reads")
                    processed += 1
                known = {r.dispute_id for r in self.store.snapshots()}
                for did in sorted(self.store.local_ids() - known):
                    self.get(did, "sweep")  # no known due date; unknown dates follow known eligible cases
                    self.store.count("sweep_reads")
                    processed += 1
                return processed
            finally:
                self.store.set_meta("last_sweep_ms", round((self.monotonic() - start) * 1000))

    def pull(self):
        with self.store.lease("pull", self.clock().timestamp(), seconds=1800):
            self.retry_pending("log")
            return Puller(self.read, self.store, self.clock).pull(lambda event: self.event(event, "log"))

    def run(self):
        """On-demand entry: correctness sweep first, then receiver and event log."""
        return {"sweep": self.sweep(), "webhook": self.receiver(), "log": self.pull()}
