"""A message addressed to a PEER agent is that agent's message, never ours.

Regression guard for the reply-ping hole found live on 2026-10-06 (message
``1557229411084931093`` in thread ``1557047016863825992``, 23:13): Discord's
reply-ping inserts the *replied-to* author into ``message.mentions`` without
anyone typing a ``<@me>`` token, so a reply to us whose typed text named a peer
used to satisfy the explicit-mention self-check and the peer veto never fired.
The veto now keys on a TYPED token (``_self_is_raw_mentioned``) extended to
pings of a role this bot actually holds, and drops any message that names a
configured peer id unless we were typed-mentioned ourselves.

Config surface: ``discord.peer_mention_ids`` (CSV) / ``DISCORD_PEER_MENTION_IDS``.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import discord
import pytest

from gateway.platforms.helpers import MessageDeduplicator
from plugins.platforms.discord.adapter import DiscordAdapter

SELF_ID = 99
PEER_USER_ID = 77
PEER_ROLE_ID = 88
OWN_ROLE_ID = 55

_next_message_id = [1000]


def _adapter(*, peers="77,88", snapshot=None, extra=None) -> DiscordAdapter:
    cfg_extra = {} if peers is None else {"peer_mention_ids": peers}
    if extra:
        cfg_extra.update(extra)
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = SimpleNamespace(value="discord")
    adapter.config = SimpleNamespace(extra=cfg_extra)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=SELF_ID, bot=True))
    adapter._dedup = MessageDeduplicator()
    adapter._is_allowed_user = Mock(return_value=True)
    adapter._warn_if_fail_closed_default = Mock()
    adapter._allowed_role_ids = set()
    if snapshot is not None:
        adapter._gate_env_snapshot = snapshot
    return adapter


def _message(adapter, content, *, mentions=None, role_mentions=None,
             reply=False, guild=None):
    _next_message_id[0] += 1
    return SimpleNamespace(
        id=_next_message_id[0],
        author=SimpleNamespace(id=42, bot=False),
        channel=SimpleNamespace(id=7),
        guild=guild,
        content=content,
        mentions=list(mentions or []),
        role_mentions=list(role_mentions or []),
        type=discord.MessageType.reply if reply else discord.MessageType.default,
    )


def _admitted(adapter, message) -> bool:
    admitted, _ = adapter._discord_message_admission(message, claim=False)
    return admitted


def test_typed_peer_user_ping_is_not_ours():
    adapter = _adapter()
    assert _admitted(adapter, _message(adapter, f"<@{PEER_USER_ID}> take this one")) is False


def test_reply_ping_from_a_human_naming_a_peer_is_not_ours():
    """The exact live failure: a reply ping puts us in ``mentions`` with no typed token."""
    adapter = _adapter()
    message = _message(
        adapter,
        f"<@{PEER_USER_ID}> can you pick this up",
        mentions=[adapter._client.user],
        reply=True,
    )
    assert _admitted(adapter, message) is False


def test_typed_self_mention_wins_over_a_peer_ping():
    adapter = _adapter()
    message = _message(adapter, f"<@{SELF_ID}> and <@{PEER_USER_ID}> both of you")
    assert _admitted(adapter, message) is True


def test_peer_role_ping_is_not_ours():
    adapter = _adapter()
    message = _message(
        adapter,
        f"<@&{PEER_ROLE_ID}> new task",
        role_mentions=[SimpleNamespace(id=PEER_ROLE_ID)],
    )
    assert _admitted(adapter, message) is False


def test_own_role_ping_counts_as_addressed_to_us():
    adapter = _adapter()
    guild = SimpleNamespace(
        get_member=Mock(return_value=SimpleNamespace(
            roles=[SimpleNamespace(id=OWN_ROLE_ID)],
        ))
    )
    message = _message(
        adapter,
        f"<@&{OWN_ROLE_ID}> and <@&{PEER_ROLE_ID}> both",
        role_mentions=[SimpleNamespace(id=OWN_ROLE_ID), SimpleNamespace(id=PEER_ROLE_ID)],
        guild=guild,
    )
    assert _admitted(adapter, message) is True


def test_env_snapshot_seeds_the_peer_ids():
    adapter = _adapter(peers=None, snapshot={"DISCORD_PEER_MENTION_IDS": str(PEER_USER_ID)})
    assert _admitted(adapter, _message(adapter, f"<@{PEER_USER_ID}> over to you")) is False


def test_no_peer_ids_configured_leaves_previous_behaviour():
    """With nothing configured the veto must stay inert — no accidental drops."""
    adapter = _adapter(peers=None, snapshot={"DISCORD_PEER_MENTION_IDS": ""})
    assert _admitted(adapter, _message(adapter, f"<@{PEER_USER_ID}> unrelated id")) is True


@pytest.mark.parametrize(
    "raw,expected",
    [("77, 88", {"77", "88"}), (["77", 88], {"77", "88"}), ("", set()), (None, set())],
)
def test_peer_mention_ids_helper_accepts_csv_and_list(raw, expected):
    adapter = _adapter(peers=None, snapshot={"DISCORD_PEER_MENTION_IDS": ""})
    adapter.config.extra["peer_mention_ids"] = raw
    assert adapter._discord_peer_mention_ids() == expected
