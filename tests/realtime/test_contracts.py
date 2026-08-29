from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
import struct
from uuid import UUID

import pytest
from pydantic import ValidationError

from server.realtime.contracts import (
    BinaryAudioFrame,
    RealtimeEnvelope,
    parse_control_json,
)


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _start(*, seq: int = 1, generation: int = 1) -> dict:
    return {
        "contract_version": "ai-realtime.contract.v1",
        "schema_version": "1.0",
        "session_id": SESSION_ID,
        "session_generation": generation,
        "producer": "backend",
        "stream_sequence": seq,
        "message_type": "session.start",
        "occurred_at": "2026-08-29T10:00:00Z",
        "payload": {
            "started_at": "2026-08-29T10:00:00Z",
            "deadline_at": "2026-08-29T11:00:00Z",
            "input_audio": {
                "encoding": "opus",
                "clock_rate_hz": 48000,
                "channels": 2,
                "frame_duration_ms": 20,
            },
            "output_audio": {
                "encoding": "opus",
                "clock_rate_hz": 48000,
                "channels": 2,
                "frame_duration_ms": 20,
            },
            "context_revision": 1,
            "context": {},
            "limits": {"max_binary_frame_bytes": 65536, "max_buffered_audio_ms": 1000},
        },
    }


def test_session_start_is_strictly_parsed() -> None:
    parsed = parse_control_json(json.dumps(_start()))
    assert isinstance(parsed, RealtimeEnvelope)
    assert parsed.session_id == UUID(SESSION_ID)
    assert parsed.message_type == "session.start"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(extra=True),
        lambda p: p.update(producer="client"),
        lambda p: p.update(session_generation=0),
        lambda p: p.update(occurred_at="2026-08-29T10:00:00"),
        lambda p: p["payload"].update(unknown=True),
    ],
)
def test_unknown_fields_and_invalid_identity_are_rejected(mutate) -> None:
    payload = _start()
    mutate(payload)
    with pytest.raises((ValidationError, ValueError)):
        parse_control_json(json.dumps(payload))


def test_duplicate_and_trailing_json_are_rejected() -> None:
    raw = json.dumps(_start())
    with pytest.raises(ValueError):
        parse_control_json(raw[:-1] + ",\"stream_sequence\":2}")
    with pytest.raises(ValueError):
        parse_control_json(raw + "{}")


def test_binary_audio_frame_roundtrips_header_and_payload() -> None:
    frame = BinaryAudioFrame(
        kind="input_opus", packet_sequence=1, rtp_timestamp=960, payload=b"opus"
    )
    raw = frame.to_bytes()
    parsed = BinaryAudioFrame.from_bytes(raw)
    assert parsed == frame
    assert raw[:4] == struct.pack(">BBH", 1, 1, 0)


@pytest.mark.parametrize(
    "raw",
    [b"", b"\x01\x03\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00x", b"\x02" * 13],
)
def test_binary_audio_frame_rejects_invalid_header(raw: bytes) -> None:
    with pytest.raises(ValueError):
        BinaryAudioFrame.from_bytes(raw)
