"""Lab-owned, path-free descriptors for immutable method assets (D17/D35).

An asset's bytes are identified by ``sha256``.  Registration and resolution are
separate operations: this descriptor never implies that the file is present or
that its terms permit redistribution.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identity import SHA256_PATTERN

ASSET_SCHEMA = "traceback.lab.asset.v1"


class AssetDescriptor(BaseModel):
    """Stable Lab metadata for one exact local asset byte sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["traceback.lab.asset.v1"] = Field(default=ASSET_SCHEMA, alias="schema")
    kind: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    sha256: str = Field(pattern=SHA256_PATTERN.pattern)
    size_bytes: int = Field(ge=0)
    origin: Literal["user", "toolchain", "lab-generated"]
    source_label: str = Field(min_length=1, max_length=160)
    terms: Literal["known", "restricted", "unknown"]
    license_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,80}$")

    @model_validator(mode="after")
    def _terms(self) -> AssetDescriptor:
        if self.terms == "known" and self.license_id is None:
            raise ValueError("known terms require a license_id")
        return self

    @property
    def asset_id(self) -> str:
        """Canonical exact-byte identity; independent of mutable provenance."""

        return f"{self.kind}:{self.sha256}"


__all__ = ["ASSET_SCHEMA", "AssetDescriptor"]
