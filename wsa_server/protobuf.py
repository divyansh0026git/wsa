"""Protobuf binary wire encoders and Gemini Argon HTTP extension header builders."""
from __future__ import annotations

import base64
import json
import time
import uuid

from .config import CONFIG

def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _encode_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return _encode_varint(len(data)) + data


def _encode_bytes(value: bytes) -> bytes:
    data = bytes(value)
    return _encode_varint(len(data)) + data


def _encode_submessage(msg: bytes) -> bytes:
    return _encode_bytes(msg)


def _build_protobuf_field(tag: int, wire_type: int, value) -> bytes:
    """Build one protobuf field. wire_type 0 is varint, 2 is length-delimited."""
    result = _encode_varint((tag << 3) | wire_type)
    if wire_type == 0:
        return result + _encode_varint(int(value))
    if wire_type != 2:
        raise ValueError(f"unsupported wire_type: {wire_type}")
    if isinstance(value, str):
        return result + _encode_string(value)
    if isinstance(value, (bytes, bytearray)):
        return result + _encode_bytes(value)
    raise TypeError(f"unsupported wire_type=2 value: {type(value)}")


def build_argon_headers(model_cfg: dict) -> dict:
    """Build the four legacy binary Protobuf headers for Argon routing."""
    model_hex = model_cfg.get("model_hex") or model_cfg.get("hex") or ""
    gemini_bl = model_cfg.get("gemini_bl")
    if gemini_bl is None:
        gemini_bl = CONFIG.get("gemini_bl") or ""
    body_mode = int(model_cfg.get("mode", 7))
    header_mode = 8 if body_mode == 7 else body_mode
    tier = model_cfg.get("slot80")
    if tier is None:
        tier = 2
    req_uuid = (model_cfg.get("req_uuid") or str(uuid.uuid4())).upper()
    session_uuid = (model_cfg.get("session_uuid") or req_uuid).upper()

    msg = bytearray()
    msg += _build_protobuf_field(1, 0, 1)
    if model_hex:
        msg += _build_protobuf_field(5, 2, model_hex)
    msg += _build_protobuf_field(7, 2, "en")
    msg += _build_protobuf_field(8, 0, 0)
    msg += _build_protobuf_field(9, 2, bytes([0x04, 0x21, 0x16, 0x05, 0x06, 0x08, 0x10, 0x13, 0x14]))
    msg += _build_protobuf_field(12, 0, 2)
    msg += _build_protobuf_field(15, 0, header_mode)
    if tier:
        msg += _build_protobuf_field(16, 0, int(tier))
    msg += _build_protobuf_field(17, 2, req_uuid)
    if gemini_bl:
        msg += _build_protobuf_field(18, 2, _build_protobuf_field(1, 2, gemini_bl))
    nested_mode = _build_protobuf_field(1, 0, body_mode)
    field20 = _build_protobuf_field(1, 2, nested_mode) + _build_protobuf_field(3, 2, "Asia/Calcutta")
    msg += _build_protobuf_field(20, 2, field20)
    if model_cfg.get("field25"):
        msg += _build_protobuf_field(25, 2, str(model_cfg["field25"]))

    session = bytearray()
    session += _build_protobuf_field(1, 2, session_uuid)
    session += _build_protobuf_field(2, 0, 1)
    return {
        "x-goog-ext-525001261-bin": base64.b64encode(bytes(msg)).decode("ascii"),
        "x-goog-ext-525005358-bin": base64.b64encode(bytes(session)).decode("ascii"),
        "x-goog-ext-73010989-bin": "CAA=",
        "x-goog-ext-73010990-bin": "CAAQABgA",
    }


def build_argon_jspb_headers(model_cfg: dict) -> dict:
    """Build the JSON-encoded JSPB equivalents used by current Argon routing."""
    model_hex = model_cfg.get("model_hex") or model_cfg.get("hex") or ""
    gemini_bl = model_cfg.get("gemini_bl")
    if gemini_bl is None:
        gemini_bl = CONFIG.get("gemini_bl") or ""
    mode = int(model_cfg.get("mode", 8))
    tier = int(model_cfg.get("slot80") or 2)
    req_uuid = (model_cfg.get("req_uuid") or str(uuid.uuid4())).upper()
    session_uuid = (model_cfg.get("session_uuid") or req_uuid).upper()
    now = time.time()
    ts_sec = int(now)
    ts_nanos = int((now - ts_sec) * 1_000_000_000)
    header_mode = mode
    jspb_main = [
        1, None, None, None, model_hex, None, "en", 0,
        [4, 33, 22, 5, 6, 8, 16, 19, 20],
        None, None, 2, None, None, header_mode, int(tier), req_uuid,
        [gemini_bl] if gemini_bl else None,
        None, [[header_mode, ts_nanos], [ts_sec, ts_nanos], "Asia/Calcutta"],
    ]
    return {
        "x-goog-ext-525001261-jspb": json.dumps(jspb_main, separators=(",", ":")),
        "x-goog-ext-525005358-jspb": json.dumps([session_uuid, 1], separators=(",", ":")),
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
    }
