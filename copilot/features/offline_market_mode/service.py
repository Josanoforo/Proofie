"""Pure copy and browser policy for offline presentation."""

OFFLINE_MESSAGE = "Offline: you can review and prepare locally. PayPal reads and approval need a connection. Sending stays manual; reconnecting never sends or retries anything."


def browser_policy() -> dict:
    return {"blocks_approval_offline": True, "blocks_send_offline": True,
            "automatic_retry": False, "automatic_send_on_reconnect": False}


def may_submit(action: str, online: bool) -> bool:
    if action not in ("approve", "send"):
        raise ValueError("Unsupported action")
    return online
