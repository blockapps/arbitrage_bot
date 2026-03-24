"""Slack error notifications with per-message cooldown."""

import os
import time
import logging
import threading

log = logging.getLogger("arb_bot")

_client = None
_channel = None
_lock = threading.Lock()
_last_sent: dict[str, float] = {}

COOLDOWN_SECONDS = 60


def _init():
    global _client, _channel
    if _client is not None:
        return

    token = os.getenv("SLACK_TOKEN", "")
    _channel = os.getenv("SLACK_OPS_CHANNEL", "")

    if not token or not _channel:
        log.warning("SLACK_TOKEN or SLACK_OPS_CHANNEL not set — Slack alerts disabled")
        return

    try:
        from slack_sdk import WebClient
        _client = WebClient(token=token)
        log.info(f"Slack notifications enabled → #{_channel}")
    except ImportError:
        log.warning("slack_sdk not installed — Slack alerts disabled")


def notify_error(message: str):
    """Send an error message to Slack, rate-limited to one per COOLDOWN_SECONDS per unique message."""
    _init()
    if _client is None:
        return

    now = time.time()
    with _lock:
        last = _last_sent.get(message, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        _last_sent[message] = now

    try:
        _client.chat_postMessage(channel=_channel, text=f":rotating_light: *Arb Bot Error*\n{message}")
    except Exception as e:
        log.warning(f"Failed to send Slack alert: {e}")
