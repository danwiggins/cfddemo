"""The locked copy-number method (signal methods CN2): its ichorCNA assets.

``mth_copy_number_ichorcna`` (family ``copy_number``) runs ichorCNA 0.5.1
from the pinned optional toolchain (CN1).  Every result is unqualified, local
and not for clinical use.

CN2 registers the three ichorCNA assets the method binds, read from the
installed package itself (``method-asset register --from-toolchain
copy-number``).  The bin size of the wigs is the locked method's: there is no
flag for it, so a registration can never disagree with the method that uses
it.

Threat model: in-process code mutation is out of scope.
"""

from __future__ import annotations

from pathlib import Path

from .references import (
    ICHOR_EXTDATA_RELPATH,
    AssetKind,
    AssetRegistrationResult,
    ichor_toolchain_files,
    register_ichor_toolchain_directory,
)

METHOD_ID = "mth_copy_number_ichorcna"
METHOD_SLUG = "copy-number-ichorcna"
# The locked bin size (spec §3.2 and §11): 1 Mb, the bin size of the adapter's
# fixtures and tests.  500 kb is the alternative (Q4).
LOCKED_BIN_SIZE_BP = 1_000_000


def locked_bin_size_bp() -> int:
    """The bin size the locked copy-number method counts and models in."""

    return LOCKED_BIN_SIZE_BP


def toolchain_tag(lock_sha256: str) -> str:
    """The 12-hex tag of a toolchain lock that its asset IDs carry."""

    return lock_sha256[:12]


def copy_number_asset_files(
    lock_sha256: str, *, bin_size_bp: int | None = None
) -> dict[AssetKind, tuple[str, str]]:
    """Each ichorCNA kind's package file name and asset ID for this toolchain."""

    return ichor_toolchain_files(
        locked_bin_size_bp() if bin_size_bp is None else bin_size_bp,
        toolchain_tag(lock_sha256),
    )


def register_toolchain_assets(
    root: Path, *, toolchain: object | None = None
) -> tuple[AssetRegistrationResult, ...]:
    """Register the installed toolchain's gc wig, map wig and centromere table.

    ``toolchain`` is a resolved ``IchorToolchain``; without one the platform's
    toolchain is resolved and verified here (TBX-TOOL-002 when it is missing
    or changed).  The caller holds the workspace mutation lock.
    """

    if toolchain is None:
        from .toolchain import resolve_copy_number_toolchain

        toolchain = resolve_copy_number_toolchain()
    identity = toolchain.identity  # type: ignore[attr-defined]
    return register_ichor_toolchain_directory(
        root,
        toolchain.r_library / ICHOR_EXTDATA_RELPATH,  # type: ignore[attr-defined]
        bin_size_bp=locked_bin_size_bp(),
        toolchain_tag=toolchain_tag(identity.lock_sha256),
    )


__all__ = [
    "LOCKED_BIN_SIZE_BP",
    "METHOD_ID",
    "METHOD_SLUG",
    "copy_number_asset_files",
    "locked_bin_size_bp",
    "register_toolchain_assets",
    "toolchain_tag",
]
