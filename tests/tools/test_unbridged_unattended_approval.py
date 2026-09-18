"""Unbridged unattended platforms must BLOCK instantly, never park on an approval.

`api_server` answers a flagged command through a real approval bridge: the run emits
``approval.request`` and accepts ``POST /v1/runs/{id}/approval`` (registered at
``gateway/platforms/api_server_runs.py``), so ``_presence()`` deliberately keeps
``is_ask`` set for it (commit 04fcf9159c).

``webhook`` and ``msgraph_webhook`` have NO such surface. The shared ``TurnRunner``
registers a notify callback for every gateway turn
(``gateway/run_turn_runner.py`` ``_run_conversation_with_approval``), so these
sessions do enter ``_await_gateway_decision`` — but nothing can resolve the webhook
session's key: ``/approve`` typed in some other chat resolves THAT chat's session
key, and a route with ``deliver: log`` never emits a card at all. The result is the
#652 shape on two surfaces: the turn stalls for the full ``approvals.timeout``
(60-300 s) and then fails closed anyway.

These tests assert the OUTCOME, not the ``is_ask`` flag. A presence-only assertion
(``is_ask is True``) passes on both the broken and the fixed tree — see
``tests/tools/test_cron_not_interactive.py``, which could not catch this — so the
webhook cases below are written to be red against the pre-fix tree: they assert the
gate never enters the wait loop at all.
"""

from unittest.mock import patch as mock_patch

import pytest

import tools.approval as approval_mod
from gateway.session_context import reset_session_vars
from tools import approval_context

PLATFORMS = ("webhook", "msgraph_webhook", "api_server")
# Flagged by DANGEROUS_PATTERNS ("stop/restart system service") and matched by no
# operator deny rule, so it reaches the approval gate instead of _floor_block.
FLAGGED_COMMAND = " ".join(["sudo", "systemctl", "restart", "nginx"])
UNATTENDED_MSG_MARKERS = ("unattended platform", "approvals.unattended_mode")


@pytest.fixture(autouse=True)
def _clear_context(monkeypatch):
    """Start from a process that is NOT already an approval context."""
    for name in ("HERMES_CRON_SESSION", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE",
                 "HERMES_EXEC_ASK", "HERMES_SINGLE_QUERY_SESSION", "HERMES_YOLO_MODE",
                 "HERMES_SESSION_PLATFORM", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(approval_mod, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_unattended_approval_mode", lambda: "deny")


@pytest.fixture(autouse=True)
def _restore_session_context(_clear_context):
    """Teardown: return session contextvars to ``_UNSET`` ("never bound here").

    ``clear_session_vars()`` leaves each var as ``""`` — an explicit "cleared" marker, not
    ``_UNSET`` — so ``get_session_env()`` stops falling back to ``os.environ`` for the rest of
    the process and a later test in the same interpreter reads the cleared value. Order-dependent
    damage: run this file FIRST and six unrelated tests fail (TestWebhookApprovalExclusion x3,
    TestApprovalTimeoutIsNotConsent x2, test_cron_not_interactive::test_cron_context_clears_leaked_presence).
    Depends on ``_clear_context`` so env scrubbing still runs first.
    """
    yield
    reset_session_vars()


def _bind(platform):
    """Bind a session exactly as a gateway process does: platform context var plus the
    process-wide ``HERMES_EXEC_ASK`` that ``start_gateway()`` sets."""
    import os

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform=platform)
    os.environ["HERMES_EXEC_ASK"] = "1"
    return tokens, clear_session_vars


def _register_notify(session_key):
    """Register the callback the shared TurnRunner registers for EVERY gateway turn —
    including one on an unbridged adapter. This is what makes the pre-fix failure a
    full-timeout stall rather than an immediate ``pending_approval``."""
    approval_mod.register_gateway_notify(session_key, lambda data: None)


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook"])
def test_unbridged_platform_blocks_without_entering_the_wait_loop(platform, monkeypatch):
    """A flagged command on webhook/msgraph_webhook resolves from unattended_mode.

    Red before the fix: with the runner's notify callback registered, the gate takes
    the ask branch and calls ``_await_gateway_decision``, blocking the turn for the
    whole approval timeout with nobody able to answer.
    """
    tokens, clear = _bind(platform)
    monkeypatch.setattr(approval_mod, "get_current_session_key", lambda *a, **k: f"sk-{platform}")
    _register_notify(f"sk-{platform}")
    try:
        with mock_patch("tools.approval._await_gateway_decision",
                        side_effect=AssertionError(
                            "unbridged platform entered the gateway wait loop")) as wait:
            result = approval_mod.check_all_command_guards(FLAGGED_COMMAND, "local")
        assert wait.call_count == 0, (
            "webhook/msgraph_webhook have no approval bridge, so the gate must never "
            "block waiting for an answer to arrive through one")
    finally:
        approval_mod.unregister_gateway_notify(f"sk-{platform}")
        clear(tokens)

    assert result["approved"] is False
    assert result.get("status") != "pending_approval", (
        "a pending approval on an unbridged platform can never be answered")
    assert result.get("approval_pending") is not True
    for marker in UNATTENDED_MSG_MARKERS:
        assert marker in (result.get("message") or ""), (
            f"the block must be loud and name the fix; missing {marker!r} in "
            f"{(result.get('message') or '')[:200]!r}")


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook"])
def test_unbridged_platform_never_parks_even_without_a_notify_callback(platform, monkeypatch):
    """Same outcome with no listener registered at all (the shape the earlier probe used)."""
    tokens, clear = _bind(platform)
    try:
        result = approval_mod.check_all_command_guards(FLAGGED_COMMAND, "local")
    finally:
        clear(tokens)

    assert result["approved"] is False
    assert result.get("status") != "pending_approval"
    assert "unattended platform" in (result.get("message") or "")


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook"])
def test_unbridged_platform_still_honours_approve_mode(platform, monkeypatch):
    """``approvals.unattended_mode: approve`` remains the operator's opt-in escape hatch."""
    tokens, clear = _bind(platform)
    monkeypatch.setattr(approval_context, "_get_unattended_approval_mode", lambda: "approve")
    try:
        result = approval_mod.check_all_command_guards(FLAGGED_COMMAND, "local")
    finally:
        clear(tokens)

    assert result["approved"] is True


def test_api_server_still_parks_for_the_runs_bridge(monkeypatch):
    """api_server MUST keep parking: ``POST /v1/runs/{id}/approval`` is a real answer.

    No notify callback is registered here, which is the state the run handler is in
    until it registers one — the pending result is what the bridge later resolves.
    """
    tokens, clear = _bind("api_server")
    try:
        result = approval_mod.check_all_command_guards(FLAGGED_COMMAND, "local")
    finally:
        clear(tokens)

    assert result.get("status") == "pending_approval", (
        f"api_server must keep its bridge; got status={result.get('status')!r} "
        f"message={(result.get('message') or '')[:200]!r}")
    assert result.get("approval_pending") is True
    assert result["approved"] is False


def test_api_server_ask_flag_survives(monkeypatch):
    """The flag the bridge depends on is unchanged (guards against a blanket clear)."""
    tokens, clear = _bind("api_server")
    try:
        _, _, _, is_ask = approval_mod._presence()
    finally:
        clear(tokens)

    assert is_ask is True


@pytest.mark.parametrize("platform", ["webhook", "msgraph_webhook"])
def test_unbridged_platform_clears_the_presence_trio(platform, monkeypatch):
    """The mechanism behind the outcome: the trio is cleared so the unattended loop runs."""
    tokens, clear = _bind(platform)
    try:
        _, is_cli, is_gateway, is_ask = approval_mod._presence()
    finally:
        clear(tokens)

    assert (is_cli, is_gateway, is_ask) == (False, False, False)
