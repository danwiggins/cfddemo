"""``traceback catalog import``: local method authority (B5a) and persisted explorer artifacts (B5b).

Every record here is generated, unqualified, local and not for clinical use.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from evidence_inspector.result_catalog import CatalogQuery
from evidence_inspector.result_trust_registry import ResultTrustRegistry
from traceback_runner import cli
from traceback_runner.fixtures import create_local_golden_path_inputs
from traceback_runner.local_authority import (
    LocalAuthorityProblem,
    LocalTrustRegistryPin,
    ensure_local_method_authority,
    local_method_version,
    open_local_result_trust_registry,
    sync_local_result_trust,
)
from traceback_runner.local_catalog import (
    build_local_explorer_artifact,
    explorer_paths,
    load_explorer_artifacts,
    open_local_catalog,
    open_local_explorer,
)
from traceback_runner.references import load_reference
from traceback_runner.serialization import canonical_json_bytes
from traceback_runner.signing import KeyPurpose, PublicTrustedKeyV2, TrustNamespace
from traceback_runner.web.explorer import CatalogAuthorityBinding, ExplorerArtifactRecord


def _main(*argv: object) -> tuple[int, dict]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main([*map(str, argv), "--json"])
    return code, json.loads(stream.getvalue())


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory):
    """One ROOT with two local records (same reference), nothing cataloged yet."""

    work = tmp_path_factory.mktemp("catalog-base")
    first = create_local_golden_path_inputs(work / "inputs-a")
    second = create_local_golden_path_inputs(work / "inputs-b", reads=600)
    root = work / "root"
    code, payload = _main("reference", "register", "--fasta", first.fasta_path, "--id", "ref", "--root", root)
    assert code == 0, payload
    record_ids = []
    for inputs in (first, second):
        code, payload = _main("run", inputs.bam_path, "--reference", "ref", "--root", root)
        assert code == 0, payload
        record_ids.append(payload["data"]["record_id"])
    assert len(set(record_ids)) == 2
    return root, tuple(record_ids), (first, second)


@pytest.fixture
def world(base, tmp_path: Path):
    root, record_ids, inputs = base
    copy = tmp_path / "root"
    shutil.copytree(root, copy, symlinks=True)
    return copy, record_ids, inputs


def _import(root: Path, record_id: str) -> tuple[int, dict]:
    return _main("catalog", "import", root / "records" / record_id, "--root", root)


def _rows(root: Path) -> list[tuple[str, str]]:
    database = root / "catalog" / "catalog.sqlite3"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return list(
            connection.execute("SELECT result_id, qualification_state FROM results ORDER BY result_id")
        )


# ---------------------------------------------------------------------------
# B5a
# ---------------------------------------------------------------------------


def test_import_catalogs_one_development_unqualified_row(world) -> None:
    root, (record_id, _), _ = world
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK, payload
    assert payload["schema_version"] == "traceback.cli-result.v2"
    assert payload["data_origin"] == "local_unqualified"
    data = payload["data"]
    assert data["qualification_state"] == "development_unqualified"
    assert data["current_provider_eligible"] is False
    assert data["trust_state"] == "development_signature_verified"
    assert data["display_role"] == "research_baseline"
    assert data["method_version"] == local_method_version("ref") == "1.0.0-local-ref"
    assert data["qualified"] is False
    assert _rows(root) == [(data["result_id"], "development_unqualified")]
    # Every store file is private.
    for path in (root / "authority").rglob("*"):
        mode = path.stat().st_mode & 0o777
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_reimport_is_idempotent_with_the_same_result_id(world) -> None:
    root, (record_id, _), _ = world
    first = _import(root, record_id)
    second = _import(root, record_id)
    assert first[0] == second[0] == cli.ExitCode.OK
    assert second[1]["data"]["result_id"] == first[1]["data"]["result_id"]
    assert len(_rows(root)) == 1


def test_bundle_signed_by_a_key_outside_the_trust_registry_is_refused(
    world, tmp_path: Path
) -> None:
    root, _, (first, _) = world
    other = tmp_path / "other-root"
    assert _main("reference", "register", "--fasta", first.fasta_path, "--id", "ref", "--root", other)[0] == 0
    code, payload = _main("run", first.bam_path, "--reference", "ref", "--root", other)
    assert code == 0, payload
    foreign = other / "records" / payload["data"]["record_id"]
    code, payload = _main("catalog", "import", foreign, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-001"
    assert _rows(root) == []
    assert not (root / "explorer").exists()


def test_tampered_method_registry_refuses_with_nothing_changed(world) -> None:
    root, (record_id, second_id), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    registry_file = root / "authority" / "ref" / "method-registry.json"
    content = registry_file.read_bytes()
    registry_file.write_bytes(content.replace(b'"registry_traceback_local"', b'"registry_traceback_locax"'))
    before = {
        path: path.read_bytes()
        for path in (*(root / "catalog").rglob("*"), *(root / "explorer").rglob("*"))
        if path.is_file()
    }
    code, payload = _import(root, second_id)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-AUTH-LOCAL-001"
    assert set(payload["data"]) >= {"code", "cause", "fix", "retryable", "docs"}
    after = {
        path: path.read_bytes()
        for path in (*(root / "catalog").rglob("*"), *(root / "explorer").rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert len(_rows(root)) == 1
    # `run` validates the same store before any copy.
    code, payload = _main("run", root / "unused.bam", "--reference", "ref", "--root", root)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["code"] == "TBX-AUTH-LOCAL-001"


@pytest.mark.parametrize("change", ("qualified", "other-definition"))
def test_a_repinned_registry_that_is_not_the_local_authority_is_refused(world, change: str) -> None:
    """Pins catch accidental edits; the rebuilt-registry comparison catches the rest."""

    from evidence_inspector.method_registry import (
        MethodRegistry,
        QualificationState,
        authority_head_for_registry,
        authority_head_sha256,
        canonical_contract_bytes,
        registry_sha256,
    )

    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    store = root / "authority" / "ref"
    registry = MethodRegistry.model_validate_json((store / "method-registry.json").read_bytes())
    if change == "qualified":
        qualified = registry.model_copy(
            update={
                "qualification_records": tuple(
                    item.model_copy(update={"state": QualificationState.QUALIFIED})
                    for item in registry.qualification_records
                )
            }
        )
    else:
        qualified = registry.model_copy(
            update={
                "method_definitions": tuple(
                    item.model_copy(update={"parameter_schema_sha256": "0" * 64})
                    for item in registry.method_definitions
                )
            }
        )
    qualified = MethodRegistry.model_validate_json(canonical_contract_bytes(qualified))
    head = authority_head_for_registry(qualified, issued_at=qualified.published_at)
    (store / "method-registry.json").write_bytes(canonical_contract_bytes(qualified))
    (store / "authority-head.json").write_bytes(canonical_contract_bytes(head))
    (store / "pins.json").write_bytes(
        canonical_json_bytes(
            {
                "authority_head_sha256": authority_head_sha256(head),
                "registry_sha256": registry_sha256(qualified),
                "schema_version": "traceback.local-authority-pins.v1",
            }
        )
    )
    registered = load_reference(root, "ref").registered
    with pytest.raises(LocalAuthorityProblem, match="nothing was changed"):
        ensure_local_method_authority(root, registered)


@pytest.mark.parametrize(
    "damage",
    ("extra-file", "pins", "mode"),
)
def test_authority_store_reopen_fails_closed(world, damage: str) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    store = root / "authority" / "ref"
    if damage == "extra-file":
        (store / "notes.json").write_text("{}")
    elif damage == "pins":
        pins = json.loads((store / "pins.json").read_bytes())
        pins["registry_sha256"] = "0" * 64
        (store / "pins.json").write_bytes(canonical_json_bytes(pins))
    else:
        (store / "authority-head.json").chmod(0o644)
    registered = load_reference(root, "ref").registered
    with pytest.raises(LocalAuthorityProblem) as raised:
        ensure_local_method_authority(root, registered)
    assert raised.value.code == "TBX-AUTH-LOCAL-001"


def test_no_authority_file_claims_qualified_or_provider_primary(world) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"qualification_state", "state"}:
                    assert item != "qualified"
                if key == "display_role":
                    assert item != "provider_primary"
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    files = [path for path in (root / "authority").rglob("*.json")]
    assert len(files) == 3
    for path in files:
        walk(json.loads(path.read_bytes()))
    registry = json.loads((root / "authority" / "ref" / "method-registry.json").read_bytes())
    assert [item["state"] for item in registry["qualification_records"]] == ["development_unqualified"]
    assert [item["display_role"] for item in registry["display_role_assignments"]] == [
        "research_baseline"
    ]


def test_import_of_a_non_bundle_or_a_synthetic_bundle_is_refused(world, tmp_path: Path) -> None:
    root, _, _ = world
    not_a_bundle = tmp_path / "not-a-bundle"
    not_a_bundle.mkdir()
    (not_a_bundle / "notes.txt").write_text("nothing")
    code, payload = _main("catalog", "import", not_a_bundle, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-001"
    demo_root = tmp_path / "demo"
    assert _main("demo", "--root", demo_root)[0] == 0
    synthetic = next(path for path in (demo_root / "records").iterdir() if not path.name.startswith("."))
    code, payload = _main("catalog", "import", synthetic, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-001"
    assert _rows(root) == []


def test_concurrent_import_is_operator_busy(world) -> None:
    root, (record_id, _), _ = world
    with cli._operator_lock(root):
        code, payload = _import(root, record_id)
    assert code == cli.ExitCode.BLOCKED
    assert payload["data"]["retryable"] is True
    assert _rows(root) == []


def test_leftover_staging_directory_is_removed(world) -> None:
    root, (record_id, _), _ = world
    staging = root / "authority" / ".staging-ref-99999"
    staging.mkdir(parents=True)
    (staging / "method-registry.json").write_text("partial")
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    assert not staging.exists()
    assert sorted(path.name for path in (root / "authority").iterdir()) == ["ref"]


def test_trust_registry_pin_recovers_forward_only_after_a_crash(world) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    pin_path = root / "trust" / "result-trust-registry.pin.json"
    stale_pin = pin_path.read_bytes()
    # Simulate a crash after a key event but before the pin update.
    from traceback_runner.signing import generate_development_keypair

    extra = generate_development_keypair(KeyPurpose.RESULT, namespace=TrustNamespace.DEVELOPMENT_LOCAL)
    with sync_local_result_trust(root) as registry:
        registry.add_key(
            PublicTrustedKeyV2(
                key_id=extra.key_id,
                namespace=TrustNamespace.DEVELOPMENT_LOCAL,
                purpose=KeyPurpose.RESULT,
                public_key_base64=base64.b64encode(extra.public_key_bytes()).decode("ascii"),
            )
        )
    pin_path.write_bytes(stale_pin)
    with pytest.raises(LocalAuthorityProblem) as raised:
        open_local_result_trust_registry(root, create=False)
    assert raised.value.code == "TBX-AUTH-LOCAL-002"
    with open_local_result_trust_registry(root, create=True) as registry:
        head = registry.current_trust().state_head_sha256
    assert LocalTrustRegistryPin.model_validate_json(pin_path.read_bytes()).state_head_sha256 == head
    # A pin that is not on the journal's chain never "recovers".
    pin = json.loads(pin_path.read_bytes())
    pin["state_head_sha256"] = "f" * 64
    pin_path.write_bytes(canonical_json_bytes(pin))
    with pytest.raises(LocalAuthorityProblem):
        open_local_result_trust_registry(root, create=True)


def test_two_references_get_separate_authorities(world, tmp_path: Path) -> None:
    root, (record_id, _), _ = world
    other = create_local_golden_path_inputs(tmp_path / "inputs-c", seed=7)
    assert _main("reference", "register", "--fasta", other.fasta_path, "--id", "ref.b", "--root", root)[0] == 0
    code, payload = _main("run", other.bam_path, "--reference", "ref.b", "--root", root)
    assert code == 0, payload
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    code, payload = _import(root, payload["data"]["record_id"])
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["method_version"] == "1.0.0-local-ref.b"
    assert sorted(path.name for path in (root / "authority").iterdir()) == ["ref", "ref.b"]
    assert len(_rows(root)) == 2


def test_catalog_rows_and_explorer_files_carry_no_input_path(world) -> None:
    root, record_ids, inputs = world
    for record_id in record_ids:
        assert _import(root, record_id)[0] == cli.ExitCode.OK
    locators = {
        str(item.fasta_path.absolute()) for item in inputs
    } | {str(item.bam_path.absolute()) for item in inputs} | {
        str(item.bam_path.absolute().parent) for item in inputs
    } | {str(root.absolute()), str(root.resolve())}
    for directory in ("catalog", "explorer", "authority"):
        for path in (root / directory).rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                for locator in locators:
                    assert locator.encode() not in content, (path, locator)
    with sqlite3.connect(root / "catalog" / "catalog.sqlite3") as connection:
        for (ref_json,) in connection.execute("SELECT ref_json FROM results"):
            text = bytes(ref_json).decode()
            assert not any(locator in text for locator in locators)


# ---------------------------------------------------------------------------
# B5b
# ---------------------------------------------------------------------------


def test_import_persists_canonical_artifact_and_binding(world) -> None:
    root, (record_id, _), _ = world
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["explorer_artifact"] == "written"
    assert payload["data"]["authority_binding"] == "written"
    result_id = payload["data"]["result_id"]
    artifact_path, binding_path = explorer_paths(root, result_id)
    for path in (artifact_path, binding_path):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    artifact = ExplorerArtifactRecord.model_validate_json(artifact_path.read_bytes())
    binding = CatalogAuthorityBinding.model_validate_json(binding_path.read_bytes())
    assert canonical_json_bytes(artifact) == artifact_path.read_bytes()
    assert canonical_json_bytes(binding) == binding_path.read_bytes()
    # Equal to a fresh rebuild from the verified bundle and the authority.
    registered = load_reference(root, "ref").registered
    authority = ensure_local_method_authority(root, registered)
    with open_local_result_trust_registry(root) as trust:
        catalog = open_local_catalog(root, trust)
        try:
            ref = catalog.query(CatalogQuery(limit=10)).results[0]
            verified, _ = catalog.verify_reference(ref)
            rebuilt = build_local_explorer_artifact(ref, verified, authority)
        finally:
            catalog.close()
    assert rebuilt == (artifact, binding)
    row = artifact.result_view.rows[0]
    assert row.qualification_state.value == "development_unqualified"
    assert row.accessible_label == "Fragment length, unqualified local record"
    assert row.qc_label == "unqualified"
    assert row.denominator.eligible_records.value == verified.measurement.eligible_alignments
    assert row.denominator.input_records.value == verified.measurement.records_scanned
    decision = artifact.result_view_request.sources[0].compatibility_decision
    assert decision.outcome.value == "unknown"
    assert not decision.delta_allowed and not decision.shared_axis_allowed


def test_library_explorer_lists_and_serves_the_imported_record(world) -> None:
    root, (record_id, _), _ = world
    with open_local_explorer(root) as explorer:
        assert explorer is None
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK
    with open_local_explorer(root) as explorer:
        assert explorer is not None and explorer.skipped == 0
        page = explorer.source.query(CatalogQuery(limit=10))
        assert [item.ref.result_id for item in page.results] == [payload["data"]["result_id"]]
        item = page.results[0]
        assert item.has_registered_view
        assert item.ref.qualification_state.value == "development_unqualified"
        assert item.eligibility.research_inspection_allowed
        assert not item.eligibility.release_explorer_allowed
        document = explorer.source.get(item.ref.result_id)
        assert document.models.catalog_ref == item.ref


@pytest.mark.parametrize("damage", ("truncate", "edit", "binding-missing", "binding-swapped"))
def test_damaged_artifact_makes_only_its_row_unavailable(world, damage: str) -> None:
    root, record_ids, _ = world
    result_ids = [_import(root, record_id)[1]["data"]["result_id"] for record_id in record_ids]
    damaged, healthy = result_ids
    artifact_path, binding_path = explorer_paths(root, damaged)
    if damage == "truncate":
        artifact_path.write_bytes(artifact_path.read_bytes()[:100])
    elif damage == "edit":
        artifact_path.write_bytes(
            artifact_path.read_bytes().replace(b"unqualified local record", b"unqualified local recorx")
        )
    elif damage == "binding-missing":
        binding_path.unlink()
    else:
        # Another result's (valid) binding under this result's name.
        binding_path.write_bytes(explorer_paths(root, healthy)[1].read_bytes())
    loaded = load_explorer_artifacts(root)
    assert loaded.skipped == 1
    with open_local_explorer(root) as explorer:
        assert explorer is not None and explorer.skipped == 1
        page = explorer.source.query(CatalogQuery(limit=10))
        views = {item.ref.result_id: item.has_registered_view for item in page.results}
        assert views == {damaged: False, healthy: True}
        with pytest.raises(KeyError, match="unavailable"):
            explorer.source.get(damaged)
        assert explorer.source.get(healthy).models.catalog_ref.result_id == healthy
    # Re-import rebuilds the damaged file from the verified record.
    code, payload = _import(root, record_ids[0])
    assert code == cli.ExitCode.OK
    expected = "written" if damage == "binding-missing" else "repaired"
    state_key = "authority_binding" if damage.startswith("binding") else "explorer_artifact"
    assert payload["data"][state_key] == expected
    assert load_explorer_artifacts(root).skipped == 0


def test_reimport_does_not_rewrite_either_file(world) -> None:
    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    paths = explorer_paths(root, result_id)
    before = [(path.stat().st_mtime_ns, path.stat().st_ino) for path in paths]
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK
    assert payload["data"]["explorer_artifact"] == "unchanged"
    assert payload["data"]["authority_binding"] == "unchanged"
    assert [(path.stat().st_mtime_ns, path.stat().st_ino) for path in paths] == before


def test_row_without_artifact_is_healed_by_reimport(world) -> None:
    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    artifact_path, _ = explorer_paths(root, result_id)
    artifact_path.unlink()  # a crash between the catalog row and the artifact
    with open_local_explorer(root) as explorer:
        assert explorer is not None
        assert not explorer.source.query(CatalogQuery(limit=10)).results[0].has_registered_view
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK
    assert payload["data"]["explorer_artifact"] == "written"
    assert payload["data"]["authority_binding"] == "unchanged"
    assert artifact_path.is_file()
    assert len(_rows(root)) == 1


def test_open_local_explorer_never_creates_trust(world) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    shutil.rmtree(root / "trust" / "result-trust-registry")
    os.unlink(root / "trust" / "result-trust-registry.pin.json")
    with pytest.raises(LocalAuthorityProblem):
        with open_local_explorer(root):
            pass
    assert not (root / "trust" / "result-trust-registry").exists()


def test_trust_registry_is_never_reached_through_a_trust_store(world) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    with open_local_result_trust_registry(root) as trust:
        assert type(trust) is ResultTrustRegistry
        catalog = open_local_catalog(root, trust)
        try:
            assert catalog.trust_store is None
            assert catalog.result_trust_registry is trust
        finally:
            catalog.close()


def test_interrupted_first_registry_creation_recovers(world) -> None:
    root, (record_id, _), _ = world
    # A registry created but never pinned (crash between the two) and empty.
    with ResultTrustRegistry(root / "trust" / "result-trust-registry", create_version=2):
        pass
    assert not (root / "trust" / "result-trust-registry.pin.json").exists()
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK, payload
    # A registry that holds events but has no pin is never discarded.
    os.unlink(root / "trust" / "result-trust-registry.pin.json")
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-AUTH-LOCAL-002"
    assert (root / "trust" / "result-trust-registry" / "registry-journal.jsonl").stat().st_size > 0


def test_explorer_file_modes_are_enforced_and_repaired(world) -> None:
    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    artifact_path, _ = explorer_paths(root, result_id)
    artifact_path.chmod(0o644)
    assert load_explorer_artifacts(root).skipped == 1
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK
    assert payload["data"]["explorer_artifact"] == "repaired"
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    assert load_explorer_artifacts(root).skipped == 0


@pytest.mark.parametrize("artifacts_directory", ("kept", "absent"))
def test_orphan_binding_is_counted_as_skipped(world, artifacts_directory: str) -> None:
    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    artifact_path = explorer_paths(root, result_id)[0]
    artifact_path.unlink()
    if artifacts_directory == "absent":  # crash during the very first import
        artifact_path.parent.rmdir()
    loaded = load_explorer_artifacts(root)
    assert loaded.records == () and loaded.skipped == 1


def test_unreadable_explorer_file_is_repaired_by_reimport(world) -> None:
    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    artifact_path = explorer_paths(root, result_id)[0]
    artifact_path.chmod(0o000)
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.OK, payload
    assert payload["data"]["explorer_artifact"] == "repaired"
    assert artifact_path.stat().st_mode & 0o777 == 0o600


def test_oversized_manifest_is_refused_without_reading_it_whole(world, tmp_path: Path) -> None:
    root, _, _ = world
    bundle = tmp_path / "huge"
    bundle.mkdir()
    with (bundle / "bundle-manifest.json").open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)  # sparse
    code, payload = _main("catalog", "import", bundle, "--root", root)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-001"


def test_e06_result_digest_binds_the_canonical_measurement(world) -> None:
    import hashlib

    from traceback_runner.bundles import verify_bundle
    from traceback_runner.signing import load_development_trust

    root, (record_id, _), _ = world
    result_id = _import(root, record_id)[1]["data"]["result_id"]
    artifact = ExplorerArtifactRecord.model_validate_json(
        explorer_paths(root, result_id)[0].read_bytes()
    )
    verified = verify_bundle(
        root / "records" / record_id,
        load_development_trust((root / "trust" / "development-result-trust.json").read_bytes()),
    )
    expected = hashlib.sha256(canonical_json_bytes(verified.measurement)).hexdigest()
    assert artifact.result_view_request.sources[0].record.result_sha256 == expected


def test_registry_mirrors_only_keys_of_imported_records(world) -> None:
    root, record_ids, _ = world
    assert _import(root, record_ids[0])[0] == cli.ExitCode.OK
    with open_local_result_trust_registry(root) as trust:
        assert len(trust.current_trust().document.keys) == 1
    assert _import(root, record_ids[1])[0] == cli.ExitCode.OK
    with open_local_result_trust_registry(root) as trust:
        assert len(trust.current_trust().document.keys) == 2


def test_reference_id_that_fails_the_public_boundary_is_refused_before_any_row(
    world, tmp_path: Path
) -> None:
    root, _, (first, _) = world
    assert _main("reference", "register", "--fasta", first.fasta_path, "--id", "patient-id", "--root", root)[0] == 0
    code, payload = _main("run", first.bam_path, "--reference", "patient-id", "--root", root)
    assert code == 0, payload
    code, payload = _import(root, payload["data"]["record_id"])
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-CAT-002"
    assert _rows(root) == []


def test_missing_registry_internals_map_to_tbx_auth_local_002(world) -> None:
    root, (record_id, _), _ = world
    assert _import(root, record_id)[0] == cli.ExitCode.OK
    os.unlink(root / "trust" / "result-trust-registry" / ".registry.lock")
    code, payload = _import(root, record_id)
    assert code == cli.ExitCode.BLOCKED, payload
    assert payload["data"]["code"] == "TBX-AUTH-LOCAL-002"
