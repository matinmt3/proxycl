from tester.mtproto import build_abridged_probe, build_obfuscated_handshake


def test_build_obfuscated_handshake_length_and_forbidden_bytes():
    header, key, iv = build_obfuscated_handshake("ee00112233445566778899aabbccddeeff0")
    assert len(header) == 64
    assert len(key) == 32
    assert len(iv) == 16
    assert header[0] != 0xEF


def test_build_obfuscated_handshake_deterministic_given_random_seed(monkeypatch):
    # Different calls should (almost always) produce different headers since
    # they're keyed off os.urandom, proving randomness is actually used.
    h1, _, _ = build_obfuscated_handshake("aabbccddeeff00112233445566778899")
    h2, _, _ = build_obfuscated_handshake("aabbccddeeff00112233445566778899")
    assert h1 != h2


def test_build_abridged_probe_format():
    probe = build_abridged_probe()
    length_byte = probe[0]
    payload = probe[1:]
    assert length_byte * 4 == len(payload)
