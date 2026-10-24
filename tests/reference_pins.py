"""Pinned digest of the reference strategy, used by the audit-tape tests.

The reference strategy over its full NQ tape (2,322,973 bars, untraced run)
gives this digest; so does the 2018-01-01 window (856,914 bars). The tape is
not shipped, so the tests that use these pins skip unless it is provided.
The pins are version 1 digests, so the text uses ``format_digest(version=1)``.
"""

from pl_transpiler.oracle import format_digest

AUDIT_REF_3_DIGEST_VERSION = 1
AUDIT_REF_3_FILLS = 7364
AUDIT_REF_3_ROUND_TURNS = 3682
AUDIT_REF_3_SHA256 = "d839e7ab8a1feb8a9e238174538f94a477d9c54193a4f48be68b6bd3284d7e0c"
AUDIT_REF_3_DIGEST = format_digest(
    fills=AUDIT_REF_3_FILLS,
    round_turns=AUDIT_REF_3_ROUND_TURNS,
    sha256=AUDIT_REF_3_SHA256,
    version=AUDIT_REF_3_DIGEST_VERSION,
)
