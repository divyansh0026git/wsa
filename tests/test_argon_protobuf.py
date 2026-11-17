"""Unit tests for Argon protobuf wire encoders and x-goog-ext-*-bin headers."""
import base64
import re
import unittest

import pytest

from wsa_server.protobuf import (
    _build_protobuf_field,
    _encode_bytes,
    _encode_string,
    _encode_submessage,
    _encode_varint,
    build_argon_headers,
)


def build_ext_525001261_bin_header(model_hex, gemini_bl):
    model_cfg = {"hex": model_hex, "gemini_bl": gemini_bl, "mode": 8, "slot80": 2, "req_uuid": "US"}
    return build_argon_headers(model_cfg)["x-goog-ext-525001261-bin"]


def build_ext_525005358_bin_header():
    return build_argon_headers({})["x-goog-ext-525005358-bin"]

# --- Wire format encoding helpers ---
class TestVarintEncoding:
    """Test varint encoding according to Protobuf wire format spec."""
    
    def test_encode_zero(self):
        """Zero should encode as single byte 0x00."""
        assert _encode_varint(0) == b'\x00'
    
    def test_encode_small_values(self):
        """Small values (0-127) should encode as single byte."""
        assert _encode_varint(1) == b'\x01'
        assert _encode_varint(2) == b'\x02'
        assert _encode_varint(8) == b'\x08'
        assert _encode_varint(127) == b'\x7f'
    
    def test_encode_multibyte_varint(self):
        """Values > 127 should use continuation bit."""
        # 300 = 0b100101100 = 0xac 0x02 (172, 2)
        assert _encode_varint(300) == b'\xac\x02'
    
    def test_encode_large_varint(self):
        """Test larger values."""
        # 16384 = 0x4000 = 0x80 0x80 0x01
        assert _encode_varint(16384) == b'\x80\x80\x01'
    
    def test_negative_raises_error(self):
        """Negative values should raise ValueError."""
        with pytest.raises(ValueError, match="varint cannot be negative"):
            _encode_varint(-1)


class TestStringEncoding:
    """Test string encoding with length prefix."""
    
    def test_encode_empty_string(self):
        """Empty string should have zero length prefix."""
        assert _encode_string("") == b'\x00'
    
    def test_encode_short_string(self):
        """Short strings should have length prefix + UTF-8 bytes."""
        # "en" = length 2, bytes 0x65 0x6e
        result = _encode_string("en")
        assert result == b'\x02en'
    
    def test_encode_longer_string(self):
        """Test longer string encoding."""
        text = "Asia/Calcutta"
        result = _encode_string(text)
        expected = bytes([len(text.encode('utf-8'))]) + text.encode('utf-8')
        assert result == expected
    
    def test_encode_hex_identifier(self):
        """Test encoding model hex identifier."""
        hex_id = "95a4c0abcba24364"
        result = _encode_string(hex_id)
        assert result[0] == len(hex_id)  # Length prefix
        assert result[1:] == hex_id.encode('utf-8')  # UTF-8 bytes
    
    def test_encode_utf8_characters(self):
        """Test UTF-8 multi-byte characters."""
        text = "你好"  # Chinese "hello"
        result = _encode_string(text)
        utf8_bytes = text.encode('utf-8')
        assert result == bytes([len(utf8_bytes)]) + utf8_bytes


class TestBytesEncoding:
    """Test bytes encoding with length prefix."""
    
    def test_encode_empty_bytes(self):
        """Empty bytes should have zero length prefix."""
        assert _encode_bytes(b'') == b'\x00'
    
    def test_encode_capability_bitmask(self):
        """Test encoding capability bitmask from spec."""
        capabilities = bytes([0x04, 0x21, 0x16, 0x05, 0x06, 0x08, 0x10, 0x13, 0x14])
        result = _encode_bytes(capabilities)
        # Should be length prefix (9) + bytes
        assert result[0] == 9
        assert result[1:] == capabilities
    
    def test_encode_arbitrary_bytes(self):
        """Test encoding arbitrary byte sequences."""
        data = b'\x00\xff\xaa\x55'
        result = _encode_bytes(data)
        assert result == b'\x04\x00\xff\xaa\x55'


class TestSubmessageEncoding:
    """Test submessage encoding (alias for bytes encoding)."""
    
    def test_encode_empty_submessage(self):
        """Empty submessage should have zero length prefix."""
        assert _encode_submessage(b'') == b'\x00'
    
    def test_encode_nested_message(self):
        """Test encoding a nested protobuf message."""
        # Create a simple nested message: Field 1 = varint 7
        nested = _build_protobuf_field(1, 0, 7)  # Tag 1, wire type 0, value 7
        result = _encode_submessage(nested)
        # Should be length prefix + nested bytes
        assert result[0] == len(nested)
        assert result[1:] == nested


class TestProtobufFieldBuilder:
    """Test complete Protobuf field construction."""
    
    def test_build_varint_field(self):
        """Test building a varint field (wire type 0)."""
        # Field 1, varint, value 1
        # Tag = (1 << 3) | 0 = 8 = 0x08
        result = _build_protobuf_field(1, 0, 1)
        assert result == b'\x08\x01'
    
    def test_build_string_field(self):
        """Test building a string field (wire type 2)."""
        # Field 5, string, value "95a4c0abcba24364"
        # Tag = (5 << 3) | 2 = 42 = 0x2a
        result = _build_protobuf_field(5, 2, "95a4c0abcba24364")
        assert result[0] == 0x2a  # Tag byte
        assert result[1] == 16  # Length of hex string
        assert result[2:] == b'95a4c0abcba24364'  # String bytes
    
    def test_build_bytes_field(self):
        """Test building a bytes field (wire type 2)."""
        # Field 9, bytes, capability bitmask
        capabilities = bytes([0x04, 0x21, 0x16, 0x05, 0x06, 0x08, 0x10, 0x13, 0x14])
        # Tag = (9 << 3) | 2 = 74 = 0x4a
        result = _build_protobuf_field(9, 2, capabilities)
        assert result[0] == 0x4a  # Tag byte
        assert result[1] == 9  # Length
        assert result[2:] == capabilities
    
    def test_build_mode_field_15(self):
        """Test building Field 15 (mode flag) as in spec."""
        # Field 15, varint, value 8 (Argon mode in header)
        # Tag = (15 << 3) | 0 = 120 = 0x78
        result = _build_protobuf_field(15, 0, 8)
        assert result == b'\x78\x08'
    
    def test_build_tier_field_16(self):
        """Test building Field 16 (tier flag) as in spec."""
        # Field 16, varint, value 2 (Argon tier)
        # Tag = (16 << 3) | 0 = 128 = 0x80 0x01 (varint encoding)
        result = _build_protobuf_field(16, 0, 2)
        assert result[0] == 0x80  # Tag first byte (128 with continuation)
        assert result[1] == 0x01  # Tag second byte
        assert result[2] == 0x02  # Value
    
    def test_build_large_field_number(self):
        """Test building field with large field number."""
        # Field 20, string, value "test"
        # Tag = (20 << 3) | 2 = 162 = 0xa2 0x01
        result = _build_protobuf_field(20, 2, "test")
        assert result[0] == 0xa2  # Tag first byte
        assert result[1] == 0x01  # Tag second byte
        assert result[2] == 4  # String length
        assert result[3:] == b'test'
    
    def test_unsupported_wire_type_raises_error(self):
        """Unsupported wire types should raise ValueError."""
        with pytest.raises(ValueError, match="unsupported wire_type"):
            _build_protobuf_field(1, 5, 123)  # Wire type 5 is invalid
    
    def test_invalid_value_type_for_wire_type_2_raises_error(self):
        """Invalid value types for length-delimited should raise TypeError."""
        with pytest.raises(TypeError, match="unsupported wire_type=2 value"):
            _build_protobuf_field(1, 2, 123)  # Integer not valid for wire type 2


class TestComplexProtobufStructures:
    """Test building complex Protobuf structures as in the spec."""
    
    def test_build_nested_submessage(self):
        """Test building a nested submessage structure."""
        # Build inner message: Field 1 (varint) = 8
        inner_msg = _build_protobuf_field(1, 0, 8)
        
        # Build outer message: Field 18 (submessage) = inner_msg
        outer_msg = _build_protobuf_field(18, 2, inner_msg)
        
        # Verify structure
        assert outer_msg[0] == 0x92  # Tag for Field 18 (18 << 3 | 2 = 146)
        assert outer_msg[1] == 0x01  # Tag second byte
        assert outer_msg[2] == len(inner_msg)  # Length of inner message
        assert outer_msg[3:] == inner_msg
    
    def test_build_multiple_fields(self):
        """Test building a message with multiple fields."""
        msg = bytearray()
        
        # Field 1 (varint): 1
        msg.extend(_build_protobuf_field(1, 0, 1))
        
        # Field 5 (string): "95a4c0abcba24364"
        msg.extend(_build_protobuf_field(5, 2, "95a4c0abcba24364"))
        
        # Field 15 (varint): 8
        msg.extend(_build_protobuf_field(15, 0, 8))
        
        # Verify we have all three fields
        result = bytes(msg)
        assert b'\x08\x01' in result  # Field 1
        assert b'95a4c0abcba24364' in result  # Field 5 value
        assert b'\x78\x08' in result  # Field 15


class TestBrowserCaptureCompatibility:
    """Test encoding produces wire format matching browser captures."""
    
    def test_fixed_capability_header_73010989(self):
        """Test x-goog-ext-73010989-bin fixed value."""
        # From spec: should be "CAA=" (Base64 of [0])
        # Build: Field 1 (varint) = 0
        msg = _build_protobuf_field(1, 0, 0)
        
        # Base64 encode
        import base64
        result = base64.b64encode(msg).decode('ascii')
        
        # Should match expected value
        assert result == "CAA="
    
    def test_fixed_capability_header_73010990(self):
        """Test x-goog-ext-73010990-bin fixed value."""
        # From spec: should be "CAAQABgA" (Base64 of [0,0,0])
        # Build message with three varint fields = 0
        msg = bytearray()
        msg.extend(_build_protobuf_field(1, 0, 0))  # Field 1 = 0
        msg.extend(_build_protobuf_field(2, 0, 0))  # Field 2 = 0
        msg.extend(_build_protobuf_field(3, 0, 0))  # Field 3 = 0
        
        # Base64 encode
        import base64
        result = base64.b64encode(bytes(msg)).decode('ascii')
        
        # Should match expected value
        assert result == "CAAQABgA"
    
    def test_argon_model_hex_field_5(self):
        """Test Field 5 encodes Argon hex identifier correctly."""
        # Field 5 (string): "95a4c0abcba24364"
        hex_id = "95a4c0abcba24364"
        result = _build_protobuf_field(5, 2, hex_id)
        
        # Verify tag byte: (5 << 3) | 2 = 42 = 0x2a
        assert result[0] == 0x2a
        
        # Verify length: 16 bytes
        assert result[1] == 16
        
        # Verify UTF-8 encoded hex string
        assert result[2:] == hex_id.encode('utf-8')
    
    def test_argon_mode_field_15(self):
        """Test Field 15 encodes Argon mode=8 correctly."""
        # Field 15 (varint): 8 (Argon mode in header)
        result = _build_protobuf_field(15, 0, 8)
        
        # Should be 0x78 0x08
        assert result == b'\x78\x08'
    
    def test_argon_tier_field_16(self):
        """Test Field 16 encodes Argon tier=2 correctly."""
        # Field 16 (varint): 2 (Argon tier)
        result = _build_protobuf_field(16, 0, 2)
        
        # Tag: (16 << 3) | 0 = 128 = 0x80 0x01 (varint)
        # Value: 2 = 0x02
        assert result == b'\x80\x01\x02'
    
    def test_capability_bitmask_field_9(self):
        """Test Field 9 encodes capability bitmask from spec."""
        # From spec: capability bitmask = 04 21 16 05 06 08 10 13 14
        capabilities = bytes([0x04, 0x21, 0x16, 0x05, 0x06, 0x08, 0x10, 0x13, 0x14])
        result = _build_protobuf_field(9, 2, capabilities)
        
        # Verify tag: (9 << 3) | 2 = 74 = 0x4a
        assert result[0] == 0x4a
        
        # Verify length: 9 bytes
        assert result[1] == 9
        
        # Verify bytes match
        assert result[2:] == capabilities
    
    def test_locale_field_7(self):
        """Test Field 7 encodes locale string."""
        # Field 7 (string): "en"
        result = _build_protobuf_field(7, 2, "en")
        
        # Tag: (7 << 3) | 2 = 58 = 0x3a
        assert result[0] == 0x3a
        
        # Length: 2
        assert result[1] == 2
        
        # UTF-8 bytes
        assert result[2:] == b'en'
    
    def test_build_label_submessage_field_18(self):
        """Test Field 18 submessage encoding."""
        # Field 18 contains submessage with Field 1 = build label string
        bl = "boq_gemini-web-uiserver_20261106.06_p0"
        
        # Build inner message: Field 1 (string) = bl
        inner = _build_protobuf_field(1, 2, bl)
        
        # Build outer message: Field 18 (submessage) = inner
        result = _build_protobuf_field(18, 2, inner)
        
        # Verify outer tag: (18 << 3) | 2 = 146 = 0x92 0x01
        assert result[0] == 0x92
        assert result[1] == 0x01
        
        # Verify contains build label
        assert bl.encode('utf-8') in result
    
    def test_complete_argon_header_sample(self):
        """Test building a complete Argon header structure."""
        # Build minimal x-goog-ext-525001261-bin message structure
        msg = bytearray()
        
        # Field 1 (varint): 1
        msg.extend(_build_protobuf_field(1, 0, 1))
        
        # Field 5 (string): Argon hex identifier
        msg.extend(_build_protobuf_field(5, 2, "95a4c0abcba24364"))
        
        # Field 7 (string): locale "en"
        msg.extend(_build_protobuf_field(7, 2, "en"))
        
        # Field 15 (varint): mode 8
        msg.extend(_build_protobuf_field(15, 0, 8))
        
        # Field 16 (varint): tier 2
        msg.extend(_build_protobuf_field(16, 0, 2))
        
        # Verify structure contains all expected components
        result = bytes(msg)
        assert b'\x08\x01' in result  # Field 1
        assert b'95a4c0abcba24364' in result  # Field 5 value
        assert b'en' in result  # Field 7 value
        assert b'\x78\x08' in result  # Field 15
        assert b'\x80\x01\x02' in result  # Field 16
        
        # Verify can be Base64 encoded
        import base64
        encoded = base64.b64encode(result).decode('ascii')
        assert len(encoded) > 0  # Non-empty Base64 string


# --- Header x-goog-ext-525001261-bin ---
def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a varint from bytes at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    pos = offset
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result, pos


def decode_string(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a length-delimited string. Returns (string, new_offset)."""
    length, pos = decode_varint(data, offset)
    string_data = data[pos:pos + length]
    return string_data.decode('utf-8'), pos + length


def decode_bytes_field(data: bytes, offset: int) -> tuple[bytes, int]:
    """Decode a length-delimited bytes field. Returns (bytes, new_offset)."""
    length, pos = decode_varint(data, offset)
    return data[pos:pos + length], pos + length


def parse_protobuf_fields(data: bytes) -> dict:
    """Parse Protobuf wire format and extract fields by tag number."""
    fields = {}
    pos = 0
    
    while pos < len(data):
        # Read field key
        key, pos = decode_varint(data, pos)
        field_num = key >> 3
        wire_type = key & 0x7
        
        if wire_type == 0:  # varint
            value, pos = decode_varint(data, pos)
            fields[field_num] = ('varint', value)
        elif wire_type == 2:  # length-delimited
            length, new_pos = decode_varint(data, pos)
            field_data = data[new_pos:new_pos + length]
            pos = new_pos + length
            
            # Try to decode as string first
            try:
                string_val = field_data.decode('utf-8')
                # Check if it's printable
                if all(c.isprintable() or c in '\n\r\t' for c in string_val):
                    fields[field_num] = ('string', string_val)
                else:
                    fields[field_num] = ('bytes', field_data)
            except UnicodeDecodeError:
                fields[field_num] = ('bytes', field_data)
        else:
            raise ValueError(f"Unsupported wire type: {wire_type}")
    
    return fields


def test_basic_structure():
    """Test that the function returns valid Base64 encoded data."""
    result = build_ext_525001261_bin_header("95a4c0abcba24364", "boq_gemini-web-uiserver_20260716")
    
    # Should be a string
    assert isinstance(result, str), "Result should be a string"
    
    # Should be valid Base64
    try:
        decoded = base64.b64decode(result)
    except Exception as e:
        raise AssertionError(f"Result is not valid Base64: {e}")
    
    # Decoded should be binary data
    assert isinstance(decoded, bytes), "Decoded result should be bytes"
    assert len(decoded) > 0, "Decoded result should not be empty"
    
    print("✓ Basic structure test passed")


def test_required_fields():
    """Test that all required fields are present with correct values."""
    model_hex = "95a4c0abcba24364"
    gemini_bl = "boq_gemini-web-uiserver_20260716"
    
    result = build_ext_525001261_bin_header(model_hex, gemini_bl)
    decoded = base64.b64decode(result)
    fields = parse_protobuf_fields(decoded)
    
    # Field 1: varint 1
    assert 1 in fields, "Field 1 should be present"
    assert fields[1] == ('varint', 1), f"Field 1 should be varint 1, got {fields[1]}"
    
    # Field 5: model hex identifier
    assert 5 in fields, "Field 5 should be present"
    assert fields[5][0] == 'string', f"Field 5 should be string, got {fields[5][0]}"
    assert fields[5][1] == model_hex, f"Field 5 should be {model_hex}, got {fields[5][1]}"
    
    # Field 7: locale "en"
    assert 7 in fields, "Field 7 should be present"
    assert fields[7] == ('string', 'en'), f"Field 7 should be 'en', got {fields[7]}"
    
    # Field 8: varint 0
    assert 8 in fields, "Field 8 should be present"
    assert fields[8] == ('varint', 0), f"Field 8 should be varint 0, got {fields[8]}"
    
    # Field 9: capability bitmask bytes
    assert 9 in fields, "Field 9 should be present"
    assert fields[9][0] == 'bytes', f"Field 9 should be bytes, got {fields[9][0]}"
    expected_caps = bytes([0x04, 0x21, 0x16, 0x05, 0x06, 0x08, 0x10, 0x13, 0x14])
    assert fields[9][1] == expected_caps, f"Field 9 capability bitmask mismatch"
    
    # Field 12: varint 2
    assert 12 in fields, "Field 12 should be present"
    assert fields[12] == ('varint', 2), f"Field 12 should be varint 2, got {fields[12]}"
    
    # Field 15: mode flag 8
    assert 15 in fields, "Field 15 should be present"
    assert fields[15] == ('varint', 8), f"Field 15 should be varint 8, got {fields[15]}"
    
    # Field 16: tier flag 2
    assert 16 in fields, "Field 16 should be present"
    assert fields[16] == ('varint', 2), f"Field 16 should be varint 2, got {fields[16]}"
    
    # Field 17: region "US"
    assert 17 in fields, "Field 17 should be present"
    assert fields[17] == ('string', 'US'), f"Field 17 should be 'US', got {fields[17]}"
    
    # Field 18: submessage with gemini_bl
    assert 18 in fields, "Field 18 should be present"
    # Field 18 is a submessage (bytes in wire format)
    # It may be parsed as string or bytes depending on content
    assert fields[18][0] in ['bytes', 'string'], f"Field 18 should be submessage, got {fields[18][0]}"
    
    # Parse Field 18 submessage - if it was decoded as string, re-encode to bytes
    if fields[18][0] == 'string':
        submsg_data = fields[18][1].encode('utf-8')
    else:
        submsg_data = fields[18][1]
    submsg_fields = parse_protobuf_fields(submsg_data)
    assert 1 in submsg_fields, "Field 18.1 should be present"
    assert submsg_fields[1][0] == 'string', f"Field 18.1 should be string, got {submsg_fields[1][0]}"
    assert submsg_fields[1][1] == gemini_bl, f"Field 18.1 should be {gemini_bl}, got {submsg_fields[1][1]}"
    
    # Field 20: submessage containing mode and timezone
    assert 20 in fields, "Field 20 should be present"
    assert fields[20][0] in ['bytes', 'string'], f"Field 20 should be bytes/string, got {fields[20][0]}"
    
    print("✓ Required fields test passed")


def test_wire_format_tags():
    """Test that field tags match expected wire format structure."""
    result = build_ext_525001261_bin_header("95a4c0abcba24364", "boq_gemini-web-uiserver_20260716")
    decoded = base64.b64decode(result)
    
    # Manually check first few bytes to verify wire format
    pos = 0
    
    # Field 1 (tag=1, wire_type=0): key = (1 << 3) | 0 = 0x08
    assert decoded[pos] == 0x08, f"Field 1 key should be 0x08, got {hex(decoded[pos])}"
    pos += 1
    assert decoded[pos] == 0x01, f"Field 1 value should be 0x01, got {hex(decoded[pos])}"
    pos += 1
    
    # Field 5 (tag=5, wire_type=2): key = (5 << 3) | 2 = 0x2A
    assert decoded[pos] == 0x2A, f"Field 5 key should be 0x2A, got {hex(decoded[pos])}"
    
    print("✓ Wire format tags test passed")


def test_empty_inputs():
    """Test behavior with empty inputs."""
    # Empty model_hex but valid gemini_bl
    result = build_ext_525001261_bin_header("", "boq_gemini-web-uiserver_20260716")
    decoded = base64.b64decode(result)
    fields = parse_protobuf_fields(decoded)
    
    # Field 5 should not be present when model_hex is empty
    assert 5 not in fields, "Field 5 should not be present when model_hex is empty"
    
    # But other required fields should still be present
    assert 1 in fields, "Field 1 should still be present"
    assert 7 in fields, "Field 7 should still be present"
    
    # Empty gemini_bl but valid model_hex
    result = build_ext_525001261_bin_header("95a4c0abcba24364", "")
    decoded = base64.b64decode(result)
    fields = parse_protobuf_fields(decoded)
    
    # Field 18 should not be present when gemini_bl is empty
    assert 18 not in fields, "Field 18 should not be present when gemini_bl is empty"
    
    # But Field 5 should be present
    assert 5 in fields, "Field 5 should be present when model_hex is provided"
    
    print("✓ Empty inputs test passed")


def test_different_values():
    """Test with different input values to ensure flexibility."""
    # Different model hex
    result = build_ext_525001261_bin_header("aabbccddeeff0011", "boq_test_123")
    decoded = base64.b64decode(result)
    fields = parse_protobuf_fields(decoded)
    
    assert fields[5][1] == "aabbccddeeff0011", "Should handle different model hex"
    
    # Parse submessage to check gemini_bl
    submsg_fields = parse_protobuf_fields(fields[18][1])
    assert submsg_fields[1][1] == "boq_test_123", "Should handle different gemini_bl"
    
    print("✓ Different values test passed")


# --- Header x-goog-ext-525005358-bin ---
class TestHeaderStructure:
    """Test basic structure and encoding of x-goog-ext-525005358-bin header."""
    
    def test_returns_base64_string(self):
        """Header builder should return valid Base64-encoded string."""
        result = build_ext_525005358_bin_header()
        
        # Should be a non-empty string
        assert isinstance(result, str)
        assert len(result) > 0
        
        # Should be valid Base64 (can decode without error)
        try:
            decoded = base64.b64decode(result)
            assert len(decoded) > 0
        except Exception as e:
            pytest.fail(f"Invalid Base64 encoding: {e}")
    
    def test_decodes_to_binary_protobuf(self):
        """Decoded result should be binary Protobuf wire format."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Should contain binary protobuf data
        assert isinstance(decoded, bytes)
        assert len(decoded) > 0
        
        # Should start with valid Protobuf field tag
        # Field 1 (string): tag = (1 << 3) | 2 = 10 = 0x0a
        assert decoded[0] == 0x0a
    
    def test_consistent_structure_across_calls(self):
        """Multiple calls should produce same structure (different UUIDs but same format)."""
        result1 = build_ext_525005358_bin_header()
        result2 = build_ext_525005358_bin_header()
        
        # Should produce different outputs (different UUIDs)
        assert result1 != result2
        
        # But same length structure
        decoded1 = base64.b64decode(result1)
        decoded2 = base64.b64decode(result2)
        
        # Both should have same length (UUID + tag overhead is fixed)
        assert len(decoded1) == len(decoded2)


class TestFieldStructure:
    """Test that the header contains correct Protobuf fields."""
    
    def test_contains_field_1_string_tag(self):
        """Should start with Field 1 (string) tag."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Field 1 (string): tag = (1 << 3) | 2 = 10 = 0x0a
        assert decoded[0] == 0x0a
    
    def test_field_1_contains_uppercase_uuid(self):
        """Field 1 should contain a valid uppercase UUID string."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Skip field tag byte (0x0a)
        # Next byte is length of UUID string (36 chars)
        assert decoded[1] == 36
        
        # Extract UUID string (36 bytes after length)
        uuid_bytes = decoded[2:38]
        uuid_str = uuid_bytes.decode('utf-8')
        
        # Verify UUID format: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
        uuid_pattern = r'^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$'
        assert re.match(uuid_pattern, uuid_str), f"Invalid UUID format: {uuid_str}"
        
        # Verify all uppercase
        assert uuid_str == uuid_str.upper(), f"UUID not uppercase: {uuid_str}"
    
    def test_contains_field_2_varint_tag(self):
        """Should contain Field 2 (varint) tag after UUID."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Field 1 structure: tag(1) + length(1) + uuid(36) = 38 bytes
        # Field 2 should start at byte 38
        # Field 2 (varint): tag = (2 << 3) | 0 = 16 = 0x10
        assert decoded[38] == 0x10
    
    def test_field_2_contains_value_1(self):
        """Field 2 should contain varint value 1."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Field 2 value should be at byte 39
        assert decoded[39] == 0x01


class TestUUIDValidation:
    """Test UUID generation and format."""
    
    def test_uuid_is_valid_format(self):
        """UUID should match standard format."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Extract UUID string
        uuid_str = decoded[2:38].decode('utf-8')
        
        # Should be 36 characters (32 hex + 4 hyphens)
        assert len(uuid_str) == 36
        
        # Should match UUID pattern
        uuid_pattern = r'^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$'
        assert re.match(uuid_pattern, uuid_str)
    
    def test_uuid_is_uppercase(self):
        """UUID should be fully uppercase."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Extract UUID string
        uuid_str = decoded[2:38].decode('utf-8')
        
        # All letters should be uppercase
        assert uuid_str == uuid_str.upper()
        
        # Should not contain any lowercase letters
        assert not any(c.islower() for c in uuid_str)
    
    def test_uuid_changes_each_call(self):
        """Each call should generate a unique UUID."""
        # Generate multiple UUIDs
        uuids = set()
        for _ in range(10):
            result = build_ext_525005358_bin_header()
            decoded = base64.b64decode(result)
            uuid_str = decoded[2:38].decode('utf-8')
            uuids.add(uuid_str)
        
        # All should be unique
        assert len(uuids) == 10
    
    def test_uuid_version_4_format(self):
        """UUID should be valid version 4 format."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        uuid_str = decoded[2:38].decode('utf-8')
        
        # UUID v4 has specific format in positions 14 and 19
        # Position 14 (version): should be '4'
        assert uuid_str[14] == '4', f"Invalid UUID version: {uuid_str[14]}"
        
        # Position 19 (variant): should be '8', '9', 'A', or 'B'
        assert uuid_str[19] in ['8', '9', 'A', 'B'], f"Invalid UUID variant: {uuid_str[19]}"


class TestCompleteHeaderValidation:
    """Test complete header structure matches specification."""
    
    def test_header_length(self):
        """Header should have expected length."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Expected structure:
        # - Field 1 tag: 1 byte (0x0a)
        # - Field 1 length: 1 byte (36)
        # - Field 1 value (UUID): 36 bytes
        # - Field 2 tag: 1 byte (0x10)
        # - Field 2 value: 1 byte (0x01)
        # Total: 40 bytes
        assert len(decoded) == 40
    
    def test_complete_wire_format(self):
        """Test complete wire format structure."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # Byte 0: Field 1 tag (0x0a)
        assert decoded[0] == 0x0a
        
        # Byte 1: UUID length (36)
        assert decoded[1] == 36
        
        # Bytes 2-37: UUID string (36 bytes)
        uuid_str = decoded[2:38].decode('utf-8')
        assert len(uuid_str) == 36
        
        # Byte 38: Field 2 tag (0x10)
        assert decoded[38] == 0x10
        
        # Byte 39: Field 2 value (0x01)
        assert decoded[39] == 0x01
    
    def test_matches_specification_schema(self):
        """Verify header matches design specification schema."""
        result = build_ext_525005358_bin_header()
        decoded = base64.b64decode(result)
        
        # From spec:
        # - Field 1 (string): uppercase UUID
        # - Field 2 (varint): 1
        
        # Verify Field 1
        assert decoded[0] == 0x0a  # Field 1 (string) tag
        assert decoded[1] == 36  # Length
        uuid_str = decoded[2:38].decode('utf-8')
        assert re.match(r'^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$', uuid_str)
        
        # Verify Field 2
        assert decoded[38] == 0x10  # Field 2 (varint) tag
        assert decoded[39] == 0x01  # Value 1


class TestBase64Encoding:
    """Test Base64 encoding properties."""
    
    def test_base64_is_valid(self):
        """Base64 output should be valid and decodable."""
        result = build_ext_525005358_bin_header()
        
        # Should not raise exception
        try:
            decoded = base64.b64decode(result)
            assert isinstance(decoded, bytes)
        except Exception as e:
            pytest.fail(f"Base64 decode failed: {e}")
    
    def test_base64_roundtrip(self):
        """Base64 encode/decode should roundtrip correctly."""
        result = build_ext_525005358_bin_header()
        
        # Decode
        decoded = base64.b64decode(result)
        
        # Re-encode
        reencoded = base64.b64encode(decoded).decode('ascii')
        
        # Should match original
        assert result == reencoded
    
    def test_base64_uses_standard_alphabet(self):
        """Base64 should use standard alphabet (not URL-safe)."""
        result = build_ext_525005358_bin_header()
        
        # Standard Base64 uses +/ not -_
        # Check that only valid standard Base64 chars are present
        valid_chars = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')
        assert all(c in valid_chars for c in result)


class TestErrorHandling:
    """Test error handling and edge cases."""
    
    def test_function_is_callable(self):
        """Function should be callable without arguments."""
        # Should not raise exception
        result = build_ext_525005358_bin_header()
        assert result is not None
    
    def test_function_takes_no_arguments(self):
        """Function should work without any arguments."""
        # Should not require any parameters
        result = build_ext_525005358_bin_header()
        assert isinstance(result, str)
    
    def test_repeated_calls_work(self):
        """Multiple successive calls should all work."""
        results = []
        for _ in range(100):
            result = build_ext_525005358_bin_header()
            assert isinstance(result, str)
            assert len(result) > 0
            results.append(result)
        
        # All should be unique (different UUIDs)
        assert len(set(results)) == 100


# --- Unified Argon headers ---
class TestBuildArgonHeaders(unittest.TestCase):
    """Test suite for build_argon_headers function."""
    
    def test_returns_dict(self):
        """Function should return a dictionary."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        self.assertIsInstance(result, dict)
    
    def test_all_four_headers_present(self):
        """All four binary Protobuf headers must be present in returned dict."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        # Verify all four headers present
        self.assertIn("x-goog-ext-525001261-bin", result)
        self.assertIn("x-goog-ext-525005358-bin", result)
        self.assertIn("x-goog-ext-73010989-bin", result)
        self.assertIn("x-goog-ext-73010990-bin", result)
    
    def test_all_headers_are_strings(self):
        """All header values should be strings."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        for key, value in result.items():
            self.assertIsInstance(value, str, f"{key} should be string")
    
    def test_all_headers_are_non_empty(self):
        """All header values should be non-empty strings."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        for key, value in result.items():
            self.assertTrue(len(value) > 0, f"{key} should be non-empty")
    
    def test_all_values_are_valid_base64(self):
        """All header values should be valid Base64-encoded strings."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        for key, value in result.items():
            try:
                decoded = base64.b64decode(value, validate=True)
                self.assertIsInstance(decoded, bytes, f"{key} should decode to bytes")
            except Exception as e:
                self.fail(f"{key} should be valid Base64: {e}")
    
    def test_fixed_header_73010989_matches_constant(self):
        """x-goog-ext-73010989-bin should match fixed constant 'CAA='."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        self.assertEqual(result["x-goog-ext-73010989-bin"], "CAA=")
    
    def test_fixed_header_73010990_matches_constant(self):
        """x-goog-ext-73010990-bin should match fixed constant 'CAAQABgA'."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        self.assertEqual(result["x-goog-ext-73010990-bin"], "CAAQABgA")
    
    def test_fixed_header_73010989_decodes_correctly(self):
        """x-goog-ext-73010989-bin should decode to expected binary value."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        decoded = base64.b64decode(result["x-goog-ext-73010989-bin"])
        # CAA= decodes to [0x08, 0x00] which is Field 1, varint, value 0
        self.assertEqual(decoded, b'\x08\x00')
    
    def test_fixed_header_73010990_decodes_correctly(self):
        """x-goog-ext-73010990-bin should decode to expected binary value."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        decoded = base64.b64decode(result["x-goog-ext-73010990-bin"])
        # CAAQABgA decodes to [0x08, 0x00, 0x10, 0x00, 0x18, 0x00]
        # Field 1 varint 0, Field 2 varint 0, Field 3 varint 0
        self.assertEqual(decoded, b'\x08\x00\x10\x00\x18\x00')
    
    def test_dynamic_header_525001261_is_generated(self):
        """x-goog-ext-525001261-bin should be dynamically generated."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        # Should be present and non-empty
        self.assertIn("x-goog-ext-525001261-bin", result)
        self.assertTrue(len(result["x-goog-ext-525001261-bin"]) > 0)
        
        # Should be valid Base64
        decoded = base64.b64decode(result["x-goog-ext-525001261-bin"])
        self.assertTrue(len(decoded) > 0)
    
    def test_dynamic_header_525005358_is_generated(self):
        """x-goog-ext-525005358-bin should be dynamically generated."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        # Should be present and non-empty
        self.assertIn("x-goog-ext-525005358-bin", result)
        self.assertTrue(len(result["x-goog-ext-525005358-bin"]) > 0)
        
        # Should be valid Base64
        decoded = base64.b64decode(result["x-goog-ext-525005358-bin"])
        self.assertTrue(len(decoded) > 0)
    
    def test_header_525005358_changes_between_calls(self):
        """x-goog-ext-525005358-bin should generate new UUID each call."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        
        result1 = build_argon_headers(model_cfg)
        result2 = build_argon_headers(model_cfg)
        
        # UUIDs should be different between calls
        self.assertNotEqual(
            result1["x-goog-ext-525005358-bin"],
            result2["x-goog-ext-525005358-bin"]
        )
    
    def test_header_525001261_uses_model_hex(self):
        """x-goog-ext-525001261-bin should contain model_hex in Field 5."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        decoded = base64.b64decode(result["x-goog-ext-525001261-bin"])
        
        # Field 5 (string) tag is 0x2a (42 = 5<<3 | 2)
        # Verify model_hex is present in decoded bytes
        hex_bytes = model_cfg["model_hex"].encode('utf-8')
        self.assertIn(hex_bytes, decoded, "model_hex should be in header")
    
    def test_header_525001261_uses_gemini_bl(self):
        """x-goog-ext-525001261-bin should contain gemini_bl in Field 18."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        decoded = base64.b64decode(result["x-goog-ext-525001261-bin"])
        
        # Verify gemini_bl is present in decoded bytes
        bl_bytes = model_cfg["gemini_bl"].encode('utf-8')
        self.assertIn(bl_bytes, decoded, "gemini_bl should be in header")
    
    def test_works_with_empty_model_hex(self):
        """Function should work with empty model_hex."""
        model_cfg = {
            "model_hex": "",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        # Should still return all four headers
        self.assertEqual(len(result), 4)
        self.assertIn("x-goog-ext-525001261-bin", result)
    
    def test_works_with_empty_gemini_bl(self):
        """Function should work with empty gemini_bl."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": ""
        }
        result = build_argon_headers(model_cfg)
        
        # Should still return all four headers
        self.assertEqual(len(result), 4)
        self.assertIn("x-goog-ext-525001261-bin", result)
    
    def test_works_with_missing_keys(self):
        """Function should work with missing model_hex/gemini_bl keys."""
        model_cfg = {}
        result = build_argon_headers(model_cfg)
        
        # Should still return all four headers with defaults
        self.assertEqual(len(result), 4)
        self.assertIn("x-goog-ext-525001261-bin", result)
        self.assertIn("x-goog-ext-525005358-bin", result)
        self.assertIn("x-goog-ext-73010989-bin", result)
        self.assertIn("x-goog-ext-73010990-bin", result)
    
    def test_different_model_hex_values(self):
        """Function should work with different model_hex values."""
        model_cfg1 = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        model_cfg2 = {
            "model_hex": "aabbccddeeff0011",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        
        result1 = build_argon_headers(model_cfg1)
        result2 = build_argon_headers(model_cfg2)
        
        # Headers should differ based on model_hex
        self.assertNotEqual(
            result1["x-goog-ext-525001261-bin"],
            result2["x-goog-ext-525001261-bin"]
        )
        
        # Fixed headers should remain the same
        self.assertEqual(
            result1["x-goog-ext-73010989-bin"],
            result2["x-goog-ext-73010989-bin"]
        )
    
    def test_different_gemini_bl_values(self):
        """Function should work with different gemini_bl values."""
        model_cfg1 = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        model_cfg2 = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260720"
        }
        
        result1 = build_argon_headers(model_cfg1)
        result2 = build_argon_headers(model_cfg2)
        
        # Headers should differ based on gemini_bl
        self.assertNotEqual(
            result1["x-goog-ext-525001261-bin"],
            result2["x-goog-ext-525001261-bin"]
        )
    
    def test_header_count_is_exactly_four(self):
        """Result should contain exactly four headers, no more, no less."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        self.assertEqual(len(result), 4, "Should return exactly 4 headers")
    
    def test_no_unexpected_keys(self):
        """Result should only contain expected header keys."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        expected_keys = {
            "x-goog-ext-525001261-bin",
            "x-goog-ext-525005358-bin",
            "x-goog-ext-73010989-bin",
            "x-goog-ext-73010990-bin"
        }
        
        self.assertEqual(set(result.keys()), expected_keys)
    
    def test_fixed_headers_consistent_across_calls(self):
        """Fixed headers should remain constant across multiple calls."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        
        results = [build_argon_headers(model_cfg) for _ in range(5)]
        
        # All fixed headers should be identical
        for i in range(1, len(results)):
            self.assertEqual(
                results[0]["x-goog-ext-73010989-bin"],
                results[i]["x-goog-ext-73010989-bin"]
            )
            self.assertEqual(
                results[0]["x-goog-ext-73010990-bin"],
                results[i]["x-goog-ext-73010990-bin"]
            )
    
    def test_realistic_argon_config(self):
        """Test with realistic Argon model configuration."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716.08_p0",
            "mode": 7,
            "think": 0,
            "slot80": 2
        }
        result = build_argon_headers(model_cfg)
        
        # Should work with extra fields in model_cfg
        self.assertEqual(len(result), 4)
        self.assertIn("x-goog-ext-525001261-bin", result)
    
    def test_base64_padding_correct(self):
        """All Base64 values should have correct padding."""
        model_cfg = {
            "model_hex": "95a4c0abcba24364",
            "gemini_bl": "boq_gemini-web-uiserver_20260716"
        }
        result = build_argon_headers(model_cfg)
        
        for key, value in result.items():
            # Base64 should end with proper padding (0-2 '=' chars)
            padding = value.count('=')
            self.assertLessEqual(padding, 2, f"{key} has too many padding chars")
            
            # If has padding, should be at end
            if '=' in value:
                last_eq = value.rfind('=')
                self.assertEqual(len(value) - 1, last_eq, 
                               f"{key} padding should be at end")

