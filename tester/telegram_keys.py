"""Pinned production Telegram trust anchors; never trust keys supplied by a proxy.

Source: TDLib commit 027780b87a66a2a0e69db644e812362bb300f1e1,
td/telegram/net/PublicRsaKeySharedMain.cpp and core.telegram.org/mtproto/auth_key.
TDLib source is distributed under the Boost Software License 1.0.
Unknown keys/prime changes fail closed and require a reviewed release.
"""

from __future__ import annotations

import hashlib

from Crypto.PublicKey import RSA

PRODUCTION_RSA_PEM = b"""-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEA6LszBcC1LGzyr992NzE0ieY+BSaOW622Aa9Bd4ZHLl+TuFQ4lo4g
5nKaMBwK/BIb9xUfg0Q29/2mgIR6Zr9krM7HjuIcCzFvDtr+L0GQjae9H0pRB2OO
62cECs5HKhT5DZ98K33vmWiLowc621dQuwKWSQKjWf50XYFw42h21P2KXUGyp2y/
+aEyZ+uVgLLQbRA1dEjSDZ2iGRy12Mk5gpYc397aYp438fsJoHIgJ2lgMv5h7WY9
t6N/byY9Nw9p21Og3AoXSL2q/2IJ1WRUhebgAdGVMlV1fkuOQoEzR7EdpqtQD9Cs
5+bfo3Nhmcyvk5ftB0WkJ9z6bNZ7yxrP8wIDAQAB
-----END RSA PUBLIC KEY-----"""


def rsa_fingerprint(n: int, e: int) -> int:
    def tl_integer(value):
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        prefix = bytes([len(raw)]) if len(raw) < 254 else b"\xfe" + len(raw).to_bytes(3, "little")
        return prefix + raw + bytes(-(len(prefix) + len(raw)) % 4)

    return int.from_bytes(hashlib.sha1(tl_integer(n) + tl_integer(e)).digest()[-8:], "little")


_public = RSA.import_key(PRODUCTION_RSA_PEM)
RSA_PUBLIC_KEYS = {rsa_fingerprint(_public.n, _public.e): (_public.n, _public.e)}
del _public

# The known safe 2048-bit prime published in the official auth-key specification.
DH_PRIME = int(
    """
C71CAEB9C6B1C9048E6C522F70F13F73980D40238E3E21C14934D037563D930F
48198A0AA7C14058229493D22530F4DBFA336F6E0AC925139543AED44CCE7C3720
FD51F69458705AC68CD4FE6B6B13ABDC9746512969328454F18FAF8C595F6424
77FE96BB2A941D5BCD1D4AC8CC49880708FA9B378E3C4F3A9060BEE67CF9A4
A4A695811051907E162753B56B0F6B410DBA74D8A84B2A14B3144E0EF128475
4FD17ED950D5965B4B9DD46582DB1178D169C6BC465B0D6FF9CA3928FEF5B9A
E4E418FC15E83EBEA0F87FA9FF5EED70050DED2849F47BF959D956850CE92985
1F0D8115F635B105EE2E4E15D04B2454BF6F4FADF034B10403119CD8E3B92FC
C5B
""".replace(" ", "").replace("\n", ""),
    16,
)
