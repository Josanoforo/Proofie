"""Prepare webhook registration; dry-run by default, execution is a human step."""
from __future__ import annotations
import argparse
import json
from urllib.parse import urlsplit

DISPUTE_EVENTS = ('CUSTOMER.DISPUTE.CREATED', 'CUSTOMER.DISPUTE.UPDATED', 'CUSTOMER.DISPUTE.RESOLVED')


def payload(url: str) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Webhook URL must be HTTPS without credentials, query or fragment')
    return {'url': url, 'event_types': [{'name': name} for name in DISPUTE_EVENTS]}


def register(client, url: str) -> str:
    result = client.call('POST', '/v1/notifications/webhooks', 'register_webhook', json_body=payload(url))
    identifier = result.get('id')
    if not isinstance(identifier, str) or not identifier or not all(c.isalnum() or c == '-' for c in identifier):
        raise ValueError('Registration response has no valid webhook id')
    return identifier


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--execute', action='store_true', help='Human-authorized sandbox registration')
    args = parser.parse_args()
    prepared = payload(args.url)
    if not args.execute:
        print(json.dumps(prepared))
        return 0
    # Lazy import: offline preparation never loads credentials or creates a transport.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pilot.client import SandboxClient
    print('COPILOT_WEBHOOK_ID=' + register(SandboxClient('hosted_webhook.jsonl'), args.url))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
