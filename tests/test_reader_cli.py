"""Local operator reader authority CLI: key custody, trust, grants, launch."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import evidence_inspector.reader_authorization_registry as registry_module
from evidence_inspector.provider_linkage_store import AuthorityTimeSource
from evidence_inspector.reader_authorization_registry import (
    SYNTHETIC_READER_AUTHORITY_ID,
    SYNTHETIC_READER_PUBLIC_KEYS,
    ReaderAuthorizationProfile,
)
from evidence_inspector.reader_authorization_synthetic import synthetic_reader_trust
from traceback_runner import cli, reader_cli

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
COHORT = "cohort_registry_" + "b" * 32
OTHER_COHORT = "cohort_registry_" + "c" * 32
SCOPE = "fragment_measurement:qty_short_fraction:unit_fraction"
OTHER_SCOPE = "copy_number:qty_tumor_fraction:unit_fraction"
SELECTOR = re.compile(r"^reader_grant_[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def fresh_profile_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_PROCESS_PROFILE", {})


class Operator:
    def __init__(self, tmp_path: Path) -> None:
        self.authority = tmp_path / "authority"
        self.registry = tmp_path / "registry"
        self.clock = AuthorityTimeSource.fixed(NOW)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def __call__(self, *argv: str, stdin=None) -> tuple[int, str, str]:
        self.out, self.err = io.StringIO(), io.StringIO()
        args = list(argv)
        if args[:2] == ["authority", "init"]:
            args += ["--registry", str(self.registry)]
        args += ["--authority-dir", str(self.authority)]
        code = reader_cli.run(
            args,
            time_source=self.clock,
            stdin=stdin,
            stdout=self.out,
            stderr=self.err,
        )
        return code, self.out.getvalue(), self.err.getvalue()

    def issue(self, *, cohort: str = COHORT, scope: str = SCOPE, days: int = 30) -> str:
        code, out, err = self(
            "grant",
            "issue",
            "--cohort",
            cohort,
            "--measurement",
            scope,
            "--expires-in-days",
            str(days),
        )
        assert code == 0, err
        selector = out.splitlines()[0]
        assert SELECTOR.fullmatch(selector)
        return selector

    def states(self) -> dict[str, str]:
        code, out, err = self("grant", "list")
        assert code == 0, err
        return dict(line.split(" ") for line in out.splitlines())

    def pins(self) -> dict:
        return json.loads((self.authority / "authority.json").read_text())


@pytest.fixture
def operator(tmp_path: Path) -> Operator:
    value = Operator(tmp_path)
    code, out, err = value("authority", "init")
    assert code == 0, err
    return value


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_init_stores_one_owner_only_key_outside_the_registry(operator) -> None:
    key = operator.authority / "reader-key-v1.pem"
    assert _mode(operator.authority) == 0o700
    assert _mode(key) == 0o600
    assert key.lstat().st_uid == os.geteuid()
    assert _mode(operator.authority / "authority.json") == 0o600
    # The private key is never printed and never inside the registry root.
    pem = key.read_bytes()
    assert b"PRIVATE KEY" in pem
    body = b"".join(pem.splitlines()[1:-1]).decode()
    assert body not in operator.out.getvalue()
    for path in operator.registry.rglob("*"):
        if path.is_file():
            assert b"PRIVATE KEY" not in path.read_bytes()
            assert body.encode() not in path.read_bytes()
    pins = operator.pins()
    assert pins["trust"]["profile"] == "provider"
    assert pins["trust"]["authority_id"] != SYNTHETIC_READER_AUTHORITY_ID
    assert pins["trust"]["keys"][0]["public_key_base64"] not in set(
        SYNTHETIC_READER_PUBLIC_KEYS.values()
    )
    assert body not in json.dumps(pins)
    assert set(pins) == {
        "registry_epoch_sha256",
        "registry_id",
        "registry_root",
        "schema_version",
        "state_head_sha256",
        "trust",
        "trust_sha256",
    }


def test_init_refuses_overlap_existing_targets_and_unsafe_directories(
    tmp_path: Path,
) -> None:
    nested = Operator(tmp_path)
    nested.authority = tmp_path / "registry" / "authority"
    code, _, err = nested("authority", "init")
    assert code == 3 and "must not overlap" in err
    nested.authority = tmp_path / "authority"
    nested.registry = tmp_path / "authority" / "registry"
    code, _, err = nested("authority", "init")
    assert code == 3 and "must not overlap" in err
    assert not (tmp_path / "authority").exists()

    first = Operator(tmp_path)
    assert first("authority", "init")[0] == 0
    code, _, err = first("authority", "init")
    assert code == 3 and "already exists" in err

    os.chmod(first.authority / "reader-key-v1.pem", 0o644)
    code, _, err = first(
        "grant", "issue", "--cohort", COHORT, "--measurement", SCOPE,
        "--expires-in-days", "1",
    )
    assert code == 3 and "owner-only" in err
    os.chmod(first.authority / "reader-key-v1.pem", 0o600)
    os.chmod(first.authority, 0o755)
    code, _, err = first("grant", "list")
    assert code == 3 and "owner-only" in err


def test_synthetic_trust_is_refused_in_the_operator_profile(operator) -> None:
    pins = operator.pins()
    pins["trust"] = json.loads(synthetic_reader_trust().model_dump_json())
    (operator.authority / "authority.json").write_text(json.dumps(pins))
    code, _, err = operator("grant", "list")
    assert code == 3
    assert "invalid" in err
    assert ReaderAuthorizationProfile.PROVIDER.value == reader_cli.PROFILE.value


def test_issue_enforces_the_90_day_maximum_and_exact_scopes(operator) -> None:
    for days in ("0", "91", "-1"):
        code, _, err = operator(
            "grant", "issue", "--cohort", COHORT, "--measurement", SCOPE,
            "--expires-in-days", days,
        )
        assert code == 3 and "1 to 90 days" in err
    for cohort, scope in (
        ("*", SCOPE),
        ("cohort_registry_*", SCOPE),
        (COHORT, "fragment_measurement:*:unit_fraction"),
        (COHORT, "fragment_measurement:qty_short_fraction"),
    ):
        code, _, err = operator(
            "grant", "issue", "--cohort", cohort, "--measurement", scope,
            "--expires-in-days", "1",
        )
        assert code == 3, (cohort, scope)
    selector = operator.issue(days=90)
    assert operator.states() == {selector: "active"}
    operator.clock.advance_to(NOW + timedelta(days=90))
    assert operator.states() == {selector: "expired"}


def test_revoke_and_list_show_selectors_and_states_only(operator) -> None:
    first = operator.issue()
    second = operator.issue(cohort=OTHER_COHORT, scope=OTHER_SCOPE)
    code, out, err = operator("grant", "revoke", first)
    assert code == 0, err
    assert out.strip() == f"{first} revoked"
    code, out, _ = operator("grant", "list")
    assert out.splitlines() == [f"{first} revoked", f"{second} active"]
    for hidden in (COHORT, OTHER_COHORT, "qty_", "unit_", "reader_authority_"):
        assert hidden not in out
    code, _, err = operator("grant", "revoke", first)
    assert code == 3
    code, _, err = operator("grant", "revoke", "reader_grant_" + "0" * 32)
    assert code == 3


def test_rotation_adds_a_key_then_revokes_the_old_one(operator) -> None:
    old = operator.issue()
    code, out, err = operator("authority", "rotate")
    assert code == 0, err
    pins = operator.pins()
    assert pins["trust"]["revision"] == 3
    assert [(key["key_version"], key["status"]) for key in pins["trust"]["keys"]] == [
        (1, "revoked"),
        (2, "active"),
    ]
    assert not (operator.authority / "reader-key-v1.pem").exists()
    assert _mode(operator.authority / "reader-key-v2.pem") == 0o600
    assert operator.states() == {old: "untrusted_key"}
    new = operator.issue()
    assert operator.states()[new] == "active"
    code, out, _ = operator("authority", "show")
    assert "key v1 revoked" in out and "key v2 active" in out


def test_recover_repins_only_a_head_that_extends_the_pin(operator) -> None:
    before = operator.pins()
    operator.issue()
    after = operator.pins()
    # Simulate a crash between the registry append and the pins write.
    (operator.authority / "authority.json").write_text(json.dumps(before))
    code, _, err = operator("grant", "list")
    assert code == 3
    code, _, err = operator("authority", "recover")
    assert code == 0, err
    assert operator.pins()["state_head_sha256"] == after["state_head_sha256"]
    assert len(operator.states()) == 1
    forged = {**before, "state_head_sha256": "0" * 64}
    (operator.authority / "authority.json").write_text(json.dumps(forged))
    code, _, err = operator("authority", "recover")
    assert code == 3 and "refusing" in err


def test_main_cli_dispatches_reader_with_a_minimal_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "reader",
            "authority",
            "init",
            "--authority-dir",
            str(tmp_path / "authority"),
            "--registry",
            str(tmp_path / "registry"),
        ]
    )
    assert code == 0
    assert "profile provider" in capsys.readouterr().out


# -- launch ------------------------------------------------------------------


def _http(origin_url: str, method: str, path: str, *, headers=None, payload=None):
    match = re.fullmatch(r"http://([0-9.]+):(\d+)", origin_url)
    assert match
    host, port = match.group(1), int(match.group(2))
    connection = http.client.HTTPConnection(host, port, timeout=5)
    body = json.dumps(payload).encode() if payload is not None else None
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", f"{host}:{port}")
    if body is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    content = response.read()
    result = (response.status, dict(response.getheaders()), content)
    connection.close()
    return result


def follow_launch_link(link: str) -> tuple[int, bytes, str, str]:
    """Do what the packaged page does with the fragment of a launch link."""

    origin, fragment = link.split("/#", 1)
    values = dict(item.split("=", 1) for item in fragment.split("&"))
    status, headers, content = _http(
        origin,
        "POST",
        "/api/v1/session/bootstrap",
        headers={"Origin": origin},
        payload={"bootstrap": values["bootstrap"]},
    )
    assert status == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    csrf = json.loads(content)["csrf_token"]
    status, headers, content = _http(
        origin,
        "POST",
        "/api/v1/session/reader-launch",
        headers={"Origin": origin, "Cookie": cookie, "X-Traceback-CSRF": csrf},
        payload={"launch": values["reader_launch"]},
    )
    assert headers["Referrer-Policy"] == "no-referrer"
    return status, content, cookie, values["reader_launch"]


class _FollowingStdin:
    """Stands in for the operator: follows each printed link, then stops."""

    def __init__(self, operator: Operator, follow: int = 1) -> None:
        self.operator = operator
        self.remaining = follow
        self.results: list[tuple[int, bytes, str, str]] = []

    def readline(self) -> str:
        links = [
            line
            for line in self.operator.out.getvalue().splitlines()
            if line.startswith("http://")
        ]
        self.results.append(follow_launch_link(links[-1]))
        self.remaining -= 1
        return "\n" if self.remaining else ""


def _runner_root(tmp_path: Path) -> str:
    """A ROOT holding an empty runner database at ``ROOT/runner/runner.sqlite3``."""

    from traceback_runner.store import JobStore

    root = tmp_path / "runner"
    JobStore(root / "runner" / "runner.sqlite3")
    return str(root)


def test_launch_prints_a_one_use_fragment_link_that_binds_over_http(
    operator, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    selector = operator.issue()
    stdin = _FollowingStdin(operator, follow=2)
    code, out, err = operator(
        "launch", "--grant", selector, "--root", _runner_root(tmp_path), stdin=stdin
    )
    assert code == 0, err
    links = [line for line in out.splitlines() if line.startswith("http://")]
    assert len(links) == 2 and links[0] != links[1]
    for link in links:
        prefix, fragment = link.split("#", 1)
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", prefix)
        assert re.fullmatch(
            r"bootstrap=[A-Za-z0-9_-]{43,}&reader_launch=[A-Za-z0-9_-]{43,}", fragment
        )
    assert [result[0] for result in stdin.results] == [200, 200]
    assert json.loads(stdin.results[0][1]) == {"reader_bound": True}
    # Nothing the server or CLI wrote outside the terminal link carries it.
    captured = capfd.readouterr()
    for _, _, _, credential in stdin.results:
        assert credential not in captured.out + captured.err
        assert credential not in err
        for path in (tmp_path / "runner").rglob("*"):
            if path.is_file():
                assert credential.encode() not in path.read_bytes()


def test_launch_refuses_an_inactive_grant(operator, tmp_path: Path) -> None:
    selector = operator.issue()
    operator("grant", "revoke", selector)
    code, out, err = operator(
        "launch", "--grant", selector, "--root", str(tmp_path / "runner"),
        stdin=io.StringIO(""),
    )
    assert code == 3 and "not active" in err
    assert "http://" not in out


def test_launch_opens_the_runner_database_demo_writes(
    operator, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from traceback_runner.store import JobStore
    from traceback_runner.web.server import RunningLocalWebService

    root = tmp_path / "root"
    assert cli.main(["demo", "--root", str(root), "--json"]) == 0
    job_id = json.loads(capsys.readouterr().out)["data"]["job_id"]
    selector = operator.issue()
    opened: list[JobStore] = []
    original_start = RunningLocalWebService.start

    def recording_start(*, store, **kwargs):
        opened.append(store)
        return original_start(store=store, **kwargs)

    monkeypatch.setattr(RunningLocalWebService, "start", recording_start)
    args = reader_cli._parser().parse_args(
        [
            "launch",
            "--grant",
            selector,
            "--root",
            str(root),
            "--authority-dir",
            str(operator.authority),
        ]
    )
    code = reader_cli._launch(args, operator.clock, io.StringIO(), io.StringIO(""))
    assert code == 0
    assert [store.path for store in opened] == [root / "runner" / "runner.sqlite3"]
    assert opened[0].get(job_id).job_id == job_id
    assert [record.job_id for record in opened[0].list_jobs()] == [job_id]
    assert not (root / "runner.sqlite3").exists()


def test_launch_without_a_runner_database_exits_4_and_creates_nothing(
    operator, tmp_path: Path
) -> None:
    selector = operator.issue()
    root = tmp_path / "empty-root"
    code, out, err = operator(
        "launch", "--grant", selector, "--root", str(root), stdin=io.StringIO("")
    )
    assert code == 4
    assert (
        "runner database not found under ROOT; "
        "run `traceback demo` or `traceback run` first"
    ) in err
    assert "http://" not in out
    assert not root.exists()


def test_rotation_stops_at_the_trust_key_bound(
    operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader_cli, "MAX_KEYS", 2)
    assert operator("authority", "rotate")[0] == 0
    code, _, err = operator("authority", "rotate")
    assert code == 3 and "15 rotations" in err
    assert (operator.authority / "reader-key-v2.pem").exists()
    assert not (operator.authority / "reader-key-v3.pem").exists()


def test_recover_refuses_a_trust_the_operator_did_not_provision(
    operator, tmp_path: Path
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from evidence_inspector.reader_authorization_registry import (
        ReaderAuthorityKey,
        ReaderKeyStatus,
        ReaderProviderTrust,
        reader_trust_sha256,
    )

    pins = operator.pins()
    trust = ReaderProviderTrust.model_validate(pins["trust"])
    foreign = ReaderProviderTrust(
        profile=trust.profile,
        authority_id=trust.authority_id,
        revision=2,
        previous_trust_sha256=reader_trust_sha256(trust),
        keys=(
            *trust.keys,
            ReaderAuthorityKey(
                key_version=2,
                public_key_base64=reader_cli._public_base64(
                    Ed25519PrivateKey.generate()
                ),
                status=ReaderKeyStatus.ACTIVE,
            ),
        ),
    )
    registry = reader_cli._open_registry(
        reader_cli.OperatorAuthorityPins.model_validate(pins), operator.clock
    )
    try:
        registry.rotate_trust(foreign, expected_trust_sha256=reader_trust_sha256(foreign))
    finally:
        registry.close()
    code, _, err = operator("authority", "recover")
    assert code == 3 and "could not be re-pinned" in err
    assert operator.pins() == pins


def test_launch_survives_too_many_unused_links(operator, tmp_path: Path) -> None:
    selector = operator.issue()

    class _Enter:
        count = 0

        def readline(self) -> str:
            self.count += 1
            return "\n" if self.count <= 17 else ""

    code, out, err = operator(
        "launch", "--grant", selector, "--root", _runner_root(tmp_path),
        stdin=_Enter(),
    )
    assert code == 0, err
    lines = out.splitlines()
    assert sum(line.startswith("http://") for line in lines) == 16
    assert "Too many unused links" in out


def test_interrupted_writes_leave_no_partial_key(operator, monkeypatch) -> None:
    real_write = os.write

    def failing_write(descriptor, data):
        raise OSError("disk full")

    monkeypatch.setattr(reader_cli.os, "write", failing_write)
    code, _, _ = operator("authority", "rotate")
    monkeypatch.setattr(reader_cli.os, "write", real_write)
    assert code == 6
    assert not (operator.authority / "reader-key-v2.pem").exists()
    assert not list(operator.authority.glob(".tmp-*"))
    assert operator("authority", "rotate")[0] == 0


def test_interrupted_rotation_is_recovered_finished_and_old_key_deleted(
    operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = operator.issue()
    real_write_pins = reader_cli._AuthorityDirectory.write_pins

    def crash(self, pins):
        raise KeyboardInterrupt

    # Crash right after the "add key" trust revision is appended.
    monkeypatch.setattr(reader_cli._AuthorityDirectory, "write_pins", crash)
    with pytest.raises(KeyboardInterrupt):
        operator("authority", "rotate")
    monkeypatch.setattr(reader_cli._AuthorityDirectory, "write_pins", real_write_pins)
    assert operator("grant", "list")[0] == 3
    code, _, err = operator("authority", "recover")
    assert code == 0, err
    pins = operator.pins()
    assert [key["status"] for key in pins["trust"]["keys"]] == ["active", "active"]
    assert operator.states() == {old: "active"}
    code, out, err = operator("authority", "rotate")
    assert code == 0, err
    assert "finished an interrupted rotation" in out
    keys = operator.pins()["trust"]["keys"]
    assert [(key["key_version"], key["status"]) for key in keys] == [
        (1, "revoked"),
        (2, "active"),
    ]
    assert not (operator.authority / "reader-key-v1.pem").exists()
    assert operator.states() == {old: "untrusted_key"}
