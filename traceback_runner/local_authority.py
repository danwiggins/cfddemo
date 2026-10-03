"""Local, unqualified method identity for ``traceback run`` records.

Milestone 1 scope: the locked local measurement policy and the method identity
a local record is signed with.  The durable local method-authority store
(``ROOT/authority``, B5a) builds on these functions; it is not created here.

Nothing in this module qualifies a method.  Every identity it returns is for an
unqualified, local development record that is not for clinical use.
"""

from __future__ import annotations

import hashlib
import re

from .contracts import (
    ApprovalState,
    BundleMethodIdentity,
    FragmentMeasurementPolicyV2,
    HistogramBin,
    RegisteredReference,
)
from .serialization import canonical_json_bytes

LOCAL_POLICY_ID = "aligned-reference-span-local-v2"
LOCAL_METHOD_ID = "mth_fragment_aligned_reference_span"
LOCAL_METHOD_BASE_VERSION = "1.0.0-local"
LOCAL_MIN_MAPPING_QUALITY = 20
LOCAL_BIN_EDGES = (0, 100, 150, 200, 300, 500, 1000)
_PRIMARY_CONTIG = re.compile(r"^chr([0-9]{1,2}|X|Y)$")


def local_fragment_policy(reference: RegisteredReference) -> FragmentMeasurementPolicyV2:
    """Return the locked ``aligned-reference-span-local-v2`` policy for one reference.

    Contigs are the registered contigs named ``chr1``..``chr99``, ``chrX`` or
    ``chrY``; when none match (for example a tiny test reference) every
    registered contig is measured.  Operators cannot change the policy.
    """

    names = tuple(contig.name for contig in reference.contigs)
    primary = tuple(name for name in names if _PRIMARY_CONTIG.fullmatch(name))
    bins = tuple(
        HistogramBin(lower_inclusive=lower, upper_exclusive=upper)
        for lower, upper in zip(LOCAL_BIN_EDGES, (*LOCAL_BIN_EDGES[1:], None), strict=True)
    )
    return FragmentMeasurementPolicyV2(
        definition_id=f"{LOCAL_POLICY_ID}.{reference.reference_id}",
        approval_state=ApprovalState.UNAPPROVED_LOCAL,
        reference_id=reference.reference_id,
        contigs=primary or names,
        min_mapping_quality=LOCAL_MIN_MAPPING_QUALITY,
        bins=bins,
    )


def local_method_identity(policy: FragmentMeasurementPolicyV2) -> BundleMethodIdentity:
    """Return the method identity bound into a local record's signed bundle.

    The version names the reference, so records made against different
    registered references never share a method version.  The definition digest
    is the SHA-256 of the canonical locked policy, so any policy change is a
    different method definition.
    """

    if type(policy) is not FragmentMeasurementPolicyV2 or (
        policy.approval_state != ApprovalState.UNAPPROVED_LOCAL
    ):
        raise ValueError("local method identity requires the locked local v2 policy")
    return BundleMethodIdentity(
        method_id=LOCAL_METHOD_ID,
        version=f"{LOCAL_METHOD_BASE_VERSION}-{policy.reference_id}",
        method_definition_sha256=hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
    )


__all__ = [
    "LOCAL_METHOD_ID",
    "LOCAL_MIN_MAPPING_QUALITY",
    "LOCAL_POLICY_ID",
    "local_fragment_policy",
    "local_method_identity",
]
