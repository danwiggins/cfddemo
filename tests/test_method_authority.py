"""Method-parameterised authority stores under ``ROOT/method-authority`` (signal SH1).

Every store here is generated, unqualified, local and not for clinical use.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evidence_inspector.method_registry import (
    AssetReference,
    DisplayRole,
    MethodDefinition,
    MethodFamily,
    MethodRegistry,
    QualificationState,
    ToolReference,
    authority_head_for_registry,
    authority_head_sha256,
    canonical_contract_bytes,
    method_definition_sha256,
    registry_sha256,
)
from traceback_runner import cli
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import (
    LocalAuthorityProblem,
    ensure_local_method_authority,
    ensure_method_authority,
    open_method_authority,
    validate_all_method_authorities,
    validate_method_authority_tree,
)
from traceback_runner.references import load_reference
from traceback_runner.serialization import canonical_json_bytes

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
SLUG = "cell-origin"


def _definition(reference_id: str = "ref", *, tool: bytes = b"tool-a") -> MethodDefinition:
    return MethodDefinition(
        method_id="mth_cell_origin_test",
        version=f"1.0.0-local-{reference_id}",
        family=MethodFamily.CELL_ORIGIN,
        quantity_id="qty_tissue_fraction",
        unit="unit_fraction",
        parameter_schema_sha256=hashlib.sha256(b"params").hexdigest(),
        tools=(
            ToolReference(
                tool_id="tool_test_caller",
                version="0.6.4",
                artifact_sha256=hashlib.sha256(tool).hexdigest(),
            ),
        ),
        assets=(
            AssetReference(
                asset_id="asset_test_atlas",
                version="1.0.0",
                content_sha256=hashlib.sha256(b"atlas").hexdigest(),
            ),
        ),
    )


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


@pytest.fixture(scope="module")
def registered(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("method-authority")
    inputs = create_local_golden_path_inputs(work / "inputs")
    root = work / "root"
    code, payload = _main(
        "reference", "register", "--fasta", inputs.fasta_path, "--id", "ref", "--root", root
    )
    assert code == 0, payload
    return root, inputs


@pytest.fixture
def root(registered, tmp_path: Path) -> Path:
    base, _ = registered
    copy = tmp_path / "root"
    shutil.copytree(base, copy, symlinks=True)
    return copy


def _snapshot(directory: Path) -> dict[str, tuple[int, bytes | None]]:
    result: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(directory.rglob("*")):
        metadata = path.lstat()
        content = path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(directory))] = (metadata.st_mode, content)
    return result


def _store(root: Path, definition: MethodDefinition) -> Path:
    return root / "method-authority" / "ref" / SLUG / method_definition_sha256(definition)


def _rewrite_store(store: Path, registry: MethodRegistry) -> None:
    """Replace a store's registry, head and pins with consistent digests."""

    head = authority_head_for_registry(registry, issued_at=registry.published_at)
    pins = json.loads((store / "pins.json").read_bytes())
    pins["registry_sha256"] = registry_sha256(registry)
    pins["authority_head_sha256"] = authority_head_sha256(head)
    (store / "method-registry.json").write_bytes(canonical_contract_bytes(registry))
    (store / "authority-head.json").write_bytes(canonical_contract_bytes(head))
    (store / "pins.json").write_bytes(canonical_json_bytes(pins))


def test_store_is_created_once_under_its_definition_hash(root: Path) -> None:
    definition = _definition()
    first = ensure_method_authority(root, "ref", SLUG, definition, now=NOW)
    store = _store(root, definition)
    assert {entry.name for entry in store.iterdir()} == {
        "method-registry.json",
        "authority-head.json",
        "pins.json",
    }
    assert oct(store.stat().st_mode & 0o777) == "0o700"
    assert all(oct(entry.stat().st_mode & 0o777) == "0o600" for entry in store.iterdir())
    before = _snapshot(store)
    second = ensure_method_authority(root, "ref", SLUG, definition, now=datetime.now(UTC))
    assert _snapshot(store) == before  # never rewritten
    assert second == first
    assert first.capability.qualification_state == QualificationState.DEVELOPMENT_UNQUALIFIED
    assert first.capability.display_role == DisplayRole.RESEARCH_BASELINE
    assert first.capability.current_provider_eligible is False
    assert first.verification_context().expected_authority_head_sha256 == (
        first.authority_head_sha256
    )


def test_builtin_authority_stays_byte_identical(root: Path) -> None:
    ensure_local_method_authority(root, load_reference(root, "ref").registered, now=NOW)
    before = _snapshot(root / "authority")
    ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    ensure_method_authority(root, "ref", "copy-number", _definition(tool=b"other"), now=NOW)
    validate_all_method_authorities(root)
    assert _snapshot(root / "authority") == before
    assert validate_all_method_authorities(root)[0] == ("ref",)


def test_tool_reinstall_appends_and_old_store_stays_valid(root: Path) -> None:
    old = _definition(tool=b"tool-a")
    new = _definition(tool=b"tool-a-reinstalled")
    ensure_method_authority(root, "ref", SLUG, old, now=NOW)
    old_bytes = _snapshot(_store(root, old))
    ensure_method_authority(root, "ref", SLUG, new, now=NOW)
    assert _snapshot(_store(root, old)) == old_bytes
    tree = validate_method_authority_tree(root)
    assert tree.damaged == ()
    hashes = {store.method_definition_sha256 for store in tree.stores}
    assert hashes == {method_definition_sha256(old), method_definition_sha256(new)}
    # The old record's binding still resolves although the "current" tool changed.
    assert tree.find("ref", SLUG, method_definition_sha256(old)) is not None


def test_tampered_store_with_consistent_pins_is_caught(root: Path) -> None:
    """The rebuilt-registry equality check, independent of ``ROOT/authority``."""

    definition = _definition()
    authority = ensure_method_authority(root, "ref", SLUG, definition, now=NOW)
    store = _store(root, definition)
    record = authority.registry.qualification_records[0]
    tampered = authority.registry.model_copy(
        update={
            "qualification_records": (
                record.model_copy(update={"approval_ref": "approval_someone_else"}),
            )
        }
    )
    _rewrite_store(store, MethodRegistry.model_validate_json(canonical_contract_bytes(tampered)))
    with pytest.raises(LocalAuthorityProblem) as raised:
        open_method_authority(root, "ref", SLUG, method_definition_sha256(definition))
    assert raised.value.code == "TBX-AUTH-LOCAL-003"
    assert "not the local unqualified authority" in raised.value.cause


@pytest.mark.timeout(60)  # a FIFO regression blocks instead of failing
@pytest.mark.parametrize(
    "damage",
    (
        "extra-file",
        "pins-digest",
        "mode",
        "truncated",
        "moved-hash",
        "moved-hash-and-pins",
        "moved-reference",
        "moved-slug",
        "fifo",
        "deep-json",
    ),
)
def test_damaged_store_hides_only_its_own_records(root: Path, damage: str) -> None:
    damaged_definition = _definition(tool=b"damaged")
    healthy = ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    other = ensure_method_authority(root, "ref", "copy-number", _definition(tool=b"cn"), now=NOW)
    ensure_method_authority(root, "ref", SLUG, damaged_definition, now=NOW)
    store = _store(root, damaged_definition)
    if damage == "extra-file":
        (store / "notes.json").write_text("{}")
    elif damage == "pins-digest":
        pins = json.loads((store / "pins.json").read_bytes())
        pins["registry_sha256"] = "0" * 64
        (store / "pins.json").write_bytes(canonical_json_bytes(pins))
    elif damage == "mode":
        (store / "authority-head.json").chmod(0o644)
    elif damage == "truncated":
        path = store / "method-registry.json"
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == "moved-hash":
        store.rename(store.parent / ("f" * 64))
        store = store.parent / ("f" * 64)
    elif damage == "deep-json":
        (store / "pins.json").write_bytes(b"[" * 200_000 + b"]" * 200_000)
    elif damage == "fifo":
        (store / "pins.json").unlink()
        os.mkfifo(store / "pins.json", 0o600)
    elif damage == "moved-hash-and-pins":
        # Pins agree with the new name; only the stored definition contradicts it.
        pins = json.loads((store / "pins.json").read_bytes())
        pins["method_definition_sha256"] = "f" * 64
        (store / "pins.json").write_bytes(canonical_json_bytes(pins))
        store = store.rename(store.parent / ("f" * 64))
    elif damage == "moved-reference":
        target = root / "method-authority" / "other" / SLUG
        target.mkdir(mode=0o700, parents=True)
        os.chmod(target.parent, 0o700)
        store = store.rename(target / store.name)
    else:
        target = root / "method-authority" / "ref" / "other-method"
        target.mkdir(mode=0o700)
        store = store.rename(target / store.name)
    tree = validate_method_authority_tree(root)
    assert {item.method_definition_sha256 for item in tree.stores} == {
        healthy.method_definition_sha256,
        other.method_definition_sha256,
    }
    assert len(tree.damaged) == 1
    assert tree.damaged[0].problem.code == "TBX-AUTH-LOCAL-003"
    assert tree.damaged[0].location == store.relative_to(root / "method-authority").parts
    assert tree.find("ref", SLUG, method_definition_sha256(damaged_definition)) is None
    # The built-in store and serve's combined check are unaffected.
    ensure_local_method_authority(root, load_reference(root, "ref").registered, now=NOW)
    combined = validate_all_method_authorities(root)[1]
    assert [item.location for item in combined.damaged] == [tree.damaged[0].location]


def test_absent_tree_is_a_no_op(root: Path) -> None:
    tree = validate_method_authority_tree(root)
    assert tree.stores == () and tree.damaged == ()
    assert not (root / "method-authority").exists()


def test_non_private_tree_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "method-authority").symlink_to(elsewhere)
    with pytest.raises(LocalAuthorityProblem) as raised:
        validate_method_authority_tree(root)
    assert raised.value.code == "TBX-AUTH-LOCAL-003"
    with pytest.raises(LocalAuthorityProblem):
        ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    assert list(elsewhere.iterdir()) == []


def test_root_with_only_method_records_is_valid(root: Path) -> None:
    assert not (root / "authority").exists()
    with pytest.raises(LocalAuthorityProblem) as raised:
        validate_all_method_authorities(root)  # neither tree: refused as before
    assert raised.value.code == "TBX-AUTH-LOCAL-001"
    ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    builtin, methods = validate_all_method_authorities(root)
    assert builtin == () and len(methods.stores) == 1
    # serve's preflight accepts the same ROOT (it needs a runner and a catalog).
    (root / "runner").mkdir()
    (root / "runner" / "runner.sqlite3").write_bytes(b"")
    (root / "catalog").mkdir()
    (root / "catalog" / "catalog.sqlite3").write_bytes(b"")
    assert cli._serve_material(root) == root / "runner" / "runner.sqlite3"


def test_damaged_builtin_store_still_refuses(root: Path) -> None:
    ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    ensure_local_method_authority(root, load_reference(root, "ref").registered, now=NOW)
    (root / "authority" / "ref" / "notes.json").write_text("{}")
    with pytest.raises(LocalAuthorityProblem) as raised:
        validate_all_method_authorities(root)
    assert raised.value.code == "TBX-AUTH-LOCAL-001"


@pytest.mark.parametrize(
    ("reference_id", "slug", "definition_reference"),
    [("ref", "Cell_Origin", "ref"), ("ref", "../x", "ref"), ("..", SLUG, "ref"), ("ref", SLUG, "hg38"), ("ref", SLUG, "other-local-ref")],
)
def test_invalid_location_or_unbound_definition_is_rejected(
    root: Path, reference_id: str, slug: str, definition_reference: str
) -> None:
    with pytest.raises(ValueError):
        ensure_method_authority(root, reference_id, slug, _definition(definition_reference), now=NOW)
    assert not (root / "method-authority").exists()


def test_run_is_not_blocked_by_a_damaged_method_store(registered, root: Path) -> None:
    _, inputs = registered
    ensure_method_authority(root, "ref", SLUG, _definition(), now=NOW)
    (_store(root, _definition()) / "notes.json").write_text("{}")
    before = _snapshot(root / "method-authority")
    code, payload = _main("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.OK, payload
    assert _snapshot(root / "method-authority") == before


def test_run_refuses_a_non_private_method_tree(registered, root: Path, tmp_path: Path) -> None:
    _, inputs = registered
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "method-authority").symlink_to(elsewhere)
    code, payload = _main("run", inputs.bam_path, "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-AUTH-LOCAL-003"
    assert not (root / "authority").exists()  # refused before anything was written
