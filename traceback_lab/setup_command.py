"""``traceback lab setup [--yes] [--only STEP,...] [--json]``: run the steps ``lab doctor`` lists.

``lab doctor`` computes the missing setup steps in order; ``lab setup`` runs
them, in that order, printing the exact command before each one.  Every
step ends in exactly one state:

- ``ok``: the step ran and succeeded.
- ``skipped``: nothing to run (a manual step such as ``brew install``, a step
  ``--only`` names that doctor already passes, or a step whose prerequisite
  step did not end ``ok``).
- ``needs_input``: the fix needs a path doctor cannot know (an atlas
  directory, a BED file, the reference FASTA).  The template command is
  printed with its placeholder and nothing is guessed: a host path is never
  auto-filled.
- ``refused``: a step that downloads or installs a licence-bearing asset was
  not confirmed.  Its FLAG line (the guide's "Setup in one command" table) is
  printed first; a TTY gets a per-step ``y`` prompt, a non-interactive run
  refuses unless ``--yes`` is given.
- ``failed``: the step ran and did not succeed; TBX-LAB-032 carries the
  scrubbed tail of its stderr.  Later steps are still attempted.

Local-only steps (building the minimap2 index, registering an already
installed toolchain's assets, removing orphaned staging) run without a
prompt.  The command is a writer: it holds the Lab writer lock for the whole
run (TBX-LAB-012 while another writer holds it) and writes one ``setup`` run
to the run log.  Exit 0 when every runnable step ended ``ok``; 8 when any
step failed, needs input or was refused.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

from traceback_runner.cli import _local_envelope, _result, _with_human

from .doctor import doctor_summary, lab_checks
from .problems import EXIT_OK, EXIT_PARTIAL, LabProblem, problem_lines, problem_payload
from .scrub import ROOT_TOKEN, BoundedStderr, Scrubber
from .store import KEEP_FAILED_ATTEMPTS, LabStore, new_run_id

OK, SKIPPED, NEEDS_INPUT, REFUSED, FAILED = "ok", "skipped", "needs_input", "refused", "failed"
STATUSES = (OK, SKIPPED, NEEDS_INPUT, REFUSED, FAILED)
LOCAL, DOWNLOAD, INPUT, MANUAL = "local", "download", "needs_input", "manual"
TAIL_LINES = 20
MMI_TIMEOUT_S = 48 * 3600.0  # the same ceiling ``lab run`` gives the index build

# The FLAG line printed before a licence-bearing step.  docs/LAB-GUIDE.md's
# "Setup in one command" table repeats each line verbatim (a test keeps them in step).
FLAGS: dict[str, str] = {
    "reference": (
        "OPERATOR-SUPPLIED REFERENCE: the hg38 FASTA is never downloaded by the Lab; "
        "obtain it through your approved local process and check its data terms"
    ),
    "ichor_toolchain": (
        "GPL/AGPL COPYLEFT: ichorCNA (R 4.4, HMMcopy, readCounter) is downloaded into the "
        "per-user toolchain cache (needs the network, about 3 GB) and runs as a separate process"
    ),
    "loyfer_assets": (
        "ATLAS DATA-USE TERMS UNKNOWN: Loyfer atlas; RESEARCH-ONLY: Loyfer UXM (Yissum); "
        "the atlas directory is operator-supplied and never downloaded"
    ),
    "pmd_asset": (
        "DATA TERMS UNKNOWN: Zhou 2018 PMD annotation; the BED.gz is operator-supplied "
        "and never downloaded"
    ),
}

# Doctor checks whose fix is an action for the operator, not a command the Lab runs.
MANUAL_STEPS = ("python", "lab_samtools", "lab_minimap2", "root", "disk", "ram", "store_version", "active")
_STEP_NAME = re.compile(r"[a-z0-9_]+")


class StepFailed(Exception):
    """A step ran and did not succeed; ``tail`` is already scrubbed."""

    def __init__(self, reason: str, *, exit_code: int | None = None, tail: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code
        self.tail = list(tail)


@dataclass
class SetupContext:
    store: LabStore
    scrubber: Scrubber
    say: Callable[[str], None]
    ask: Callable[[str], None]
    yes: bool
    interactive: bool
    echo_child: bool


@dataclass(frozen=True)
class StepSpec:
    """How one doctor check is turned into a setup step."""

    kind: str
    run: Callable[[SetupContext], str | None] | None = None
    command: Callable[[SetupContext, dict[str, Any]], str] | None = None
    placeholder: str | None = None
    after: tuple[str, ...] = ()


@dataclass
class StepResult:
    name: str
    kind: str
    status: str
    reason: str
    command: str
    flag: str | None = None
    exit_code: int | None = None
    tail: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "command": self.command,
            "flag": self.flag,
            "exit_code": self.exit_code,
            "stderr_tail": list(self.tail),
        }


@dataclass(frozen=True)
class PlannedStep:
    name: str
    check: dict[str, Any]
    spec: StepSpec


# -- runners ------------------------------------------------------------------------------
def _child_argv(argv: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "traceback_runner", *argv]


def _pump(stream: IO[str], keep: BoundedStderr, echo: Callable[[str], None] | None) -> None:
    for line in stream:
        keep.add(line)
        if echo is not None:
            echo(line.rstrip("\n"))


def run_signed(context: SetupContext, argv: Sequence[str]) -> str:
    """Run one signed ``traceback`` command as a child; a non-zero exit is a failed step.

    Raw child output is only echoed to the operator's own terminal (human mode);
    the bounded tails persisted with TBX-LAB-032 pass through the scrubber.
    """

    stdout_keep = BoundedStderr(head=0, tail=TAIL_LINES)
    stderr_keep = BoundedStderr(head=0, tail=TAIL_LINES)
    echo = (lambda line: print(line, flush=True)) if context.echo_child else None
    try:
        process = subprocess.Popen(
            _child_argv(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
        )
    except OSError as exc:
        raise StepFailed(f"could not start the command ({type(exc).__name__})") from exc
    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, stdout_keep, echo), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, stderr_keep, None), daemon=True),
    ]
    for thread in threads:
        thread.start()
    code = process.wait()
    for thread in threads:
        thread.join()
    if code != 0:
        tail = stderr_keep.scrubbed(context.scrubber) or stdout_keep.scrubbed(context.scrubber)
        raise StepFailed(f"exit {code}", exit_code=code, tail=tail)
    return "command exited 0"


def _run_ichor_toolchain(context: SetupContext) -> str:
    run_signed(context, ["toolchain", "install", "ichor", "--yes"])
    return "ichorCNA toolchain installed and verified"


def _run_ichor_assets(context: SetupContext) -> str:
    run_signed(context, ["method-asset", "register", "--from-toolchain", "copy-number", "--root", str(context.store.root)])
    return "ichorCNA method assets registered"


def _run_mmi(context: SetupContext) -> str:
    from .ingest import ensure_mmi, resolve_reference
    from .reference_integrity import verify_reference_source
    from .tools import find_minimap2

    minimap2 = find_minimap2()
    if minimap2.path is None:
        raise StepFailed("minimap2 is not on PATH")
    reference = resolve_reference(context.store.root, None)
    proof = verify_reference_source(context.store, reference, write_cache=True)
    context.say("mmi   building the minimap2 index once (hg38: ~20-40 min, ~8 GB)")
    ensure_mmi(
        context.store, reference, minimap2, reference_proof=proof, override=None,
        scrubber=context.scrubber, timeout_s=MMI_TIMEOUT_S,
    )
    return f"minimap2 index built for reference {reference.reference_id}"


def _mmi_command(context: SetupContext, check: dict[str, Any]) -> str:
    from .ingest import mmi_path, resolve_reference

    try:
        reference = resolve_reference(context.store.root, None)
    except LabProblem:
        return "minimap2 -x map-ont -d <ROOT>/lab/refs/<reference sha256>.mmi <registered FASTA>"
    target = mmi_path(context.store, reference)
    return f"minimap2 -x map-ont -d <ROOT>/lab/refs/{target.name} <FASTA of reference {reference.reference_id}>"


def _run_staging(context: SetupContext) -> str:
    from .gc_command import plan_gc, remove_item

    plan = plan_gc(context.store, keep=KEEP_FAILED_ATTEMPTS, locked=True, staging_only=True)
    outcomes = [remove_item(context.store, item) for item in plan.items]
    failed = [outcome for outcome in outcomes if not outcome.removed]
    if failed:
        raise StepFailed(
            f"{len(failed)} of {len(outcomes)} staging director{'y' if len(outcomes) == 1 else 'ies'} not removed",
            tail=[context.scrubber.line(f"{outcome.item.label}  {outcome.remaining_bytes:,} B still held") for outcome in failed],
        )
    return f"{len(outcomes)} orphaned staging director{'y' if len(outcomes) == 1 else 'ies'} removed"


CATALOGUE: dict[str, StepSpec] = {
    "reference": StepSpec(INPUT, placeholder="hg38.fa"),
    "mmi": StepSpec(LOCAL, run=_run_mmi, command=_mmi_command),
    "ichor_toolchain": StepSpec(
        DOWNLOAD, run=_run_ichor_toolchain, command=lambda context, check: "traceback toolchain install ichor --yes",
    ),
    "ichor_assets": StepSpec(
        LOCAL, run=_run_ichor_assets, after=("ichor_toolchain",),
        command=lambda context, check: "traceback method-asset register --from-toolchain copy-number --root <ROOT>",
    ),
    "loyfer_assets": StepSpec(INPUT, placeholder="LOYFER_DIR"),
    "pmd_asset": StepSpec(INPUT, placeholder="YOUR_LOCAL_PMD_coordinates_hg38.bed.gz"),
    "staging": StepSpec(
        LOCAL, run=_run_staging, command=lambda context, check: "traceback lab gc --staging-only --apply --root <ROOT>",
    ),
}
KNOWN_STEPS = (*MANUAL_STEPS, *CATALOGUE)


def step_names(text: str) -> list[str]:
    """``--only``: comma-separated doctor check names (``signed_*`` names are dynamic)."""

    names = [name.strip() for name in text.split(",") if name.strip()]
    unknown = [name for name in names if name not in KNOWN_STEPS and not (name.startswith("signed_") and _STEP_NAME.fullmatch(name))]
    if not names or unknown:
        raise argparse.ArgumentTypeError(
            f"--only takes doctor check names (unknown: {', '.join(unknown) or 'none given'}); "
            f"known: {', '.join(KNOWN_STEPS)}, signed_*"
        )
    return names


def spec_for(name: str, check: dict[str, Any]) -> StepSpec:
    spec = CATALOGUE.get(name)
    if spec is None:
        return StepSpec(MANUAL)
    if spec.kind == INPUT and (spec.placeholder is None or spec.placeholder not in str(check.get("fix", ""))):
        # The fix is a restore/inspect instruction, not the registration template.
        return StepSpec(MANUAL)
    return spec


def plan_steps(checks: Sequence[dict[str, Any]], only: Sequence[str] | None) -> list[PlannedStep]:
    """Doctor's non-pass checks with a fix, in doctor's order, filtered by ``--only``."""

    plan = [
        PlannedStep(check["name"], check, spec_for(check["name"], check))
        for check in checks
        if check.get("status") != "pass" and check.get("fix")
    ]
    if only is not None:
        plan = [step for step in plan if step.name in only]
    return plan


def _interactive() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _confirm(context: SetupContext, name: str) -> tuple[bool, str]:
    if context.yes:
        return True, "confirmed by --yes"
    if not context.interactive:
        return False, "downloads; not confirmed (non-interactive and no --yes)"
    context.ask(f"Run the {name} step? It downloads or installs the flagged asset. [y/N] ")
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer in {"y", "yes"}:
        return True, "confirmed at the prompt"
    return False, "declined at the prompt"


def execute(step: PlannedStep, context: SetupContext, done: Sequence[StepResult]) -> StepResult:
    """Run one planned step; never raises for a failing step."""

    spec = step.spec
    template = spec.command(context, step.check) if spec.command is not None else str(step.check["fix"])
    shown = show(template, context.store)
    flag = FLAGS.get(step.name)
    result = StepResult(step.name, spec.kind, SKIPPED, "", template, flag)
    if flag is not None:
        context.say(f"FLAG  {flag}")
    if spec.kind == MANUAL:
        result.reason = f"manual; run it yourself: {template}"
        return result
    if spec.kind == INPUT:
        context.say(f"TEMPLATE  {shown}")
        result.status = NEEDS_INPUT
        result.reason = f"replace {spec.placeholder} with your local path, then run: {template}"
        return result
    blocked = [
        earlier.name for earlier in done
        if earlier.name in spec.after and earlier.status != OK
    ]
    if blocked:
        result.reason = f"needs {', '.join(blocked)} first ({', '.join(e.status for e in done if e.name in blocked)})"
        return result
    if spec.kind == DOWNLOAD:
        confirmed, why = _confirm(context, step.name)
        if not confirmed:
            result.status = REFUSED
            result.reason = f"{why}; run: {template}"
            return result
    context.say(f"RUN   {shown}")
    try:
        assert spec.run is not None
        summary = spec.run(context)
    except StepFailed as failure:
        result.status, result.reason = FAILED, context.scrubber.line(failure.reason)
        result.exit_code, result.tail = failure.exit_code, failure.tail
    except LabProblem as problem:
        result.status = FAILED
        result.reason = context.scrubber.line(f"{problem.code}: {problem.cause}")
        result.tail = [context.scrubber.line(f"fix: {problem.fix}")]
    except Exception as exc:  # noqa: BLE001 - a failing step never stops the later ones
        result.status = FAILED
        result.reason = f"{type(exc).__name__} while running the step"
        result.tail = context.scrubber.text(str(exc)).splitlines()[-TAIL_LINES:]
    else:
        result.status, result.reason = OK, summary or "done"
    return result


def show(template: str, store: LabStore) -> str:
    """The human form of a ``<ROOT>`` template (JSON and the run log keep the token)."""

    return template.replace(ROOT_TOKEN, shlex.quote(str(store.root)))


def step_line(result: StepResult, store: LabStore) -> str:
    return f"STEP  {result.name}  {result.status}  {show(result.reason, store)}".rstrip()


def add_setup_parser(commands: Any, common: Callable[[argparse.ArgumentParser], None]) -> None:
    setup = commands.add_parser(
        "setup",
        help="run the setup steps lab doctor lists; asks before each download",
        description=(
            "Runs lab doctor, then each fixable step in doctor's order, printing the exact command "
            "first. Steps that download or install a licence-bearing asset print their FLAG line and "
            "need a per-step confirmation (y on a TTY, or --yes). Steps that need a path doctor cannot "
            "know print their template and are marked needs_input; no host path is ever guessed. "
            "Takes the Lab writer lock."
        ),
    )
    setup.add_argument("--yes", action="store_true", help="confirm every download step (non-interactive use)")
    setup.add_argument(
        "--only", type=step_names, default=None, metavar="STEP,...",
        help="only these doctor check names (for example mmi,ichor_toolchain,staging)",
    )
    common(setup)
    setup.set_defaults(handler=setup_command)


def setup_command(args: argparse.Namespace, store: LabStore) -> tuple[int, dict[str, Any]]:
    as_json = bool(args.as_json)
    only = list(args.only) if args.only is not None else None

    def say(line: str) -> None:
        if not as_json:
            print(line, flush=True)

    def ask(text: str) -> None:
        print(text, end="", file=sys.stderr if as_json else sys.stdout, flush=True)

    context = SetupContext(
        store=store, scrubber=Scrubber(root=store.root), say=say, ask=ask,
        yes=bool(args.yes), interactive=_interactive(), echo_child=not as_json,
    )
    run_id = new_run_id()
    results: list[StepResult] = []
    with store.writer("setup", run_id, step="doctor") as lock:
        log = store.run_log(run_id)
        log.append("run_start", command="setup", pid=os.getpid(), yes=bool(args.yes), only=only)
        say(f"ROOT  {store.root}")
        before = lab_checks(store)
        plan = plan_steps(before, only)
        for name in only or ():
            if name not in {step.name for step in plan}:
                kind = CATALOGUE.get(name, StepSpec(MANUAL)).kind
                results.append(StepResult(name, kind, SKIPPED, "nothing to do: doctor passes this check", "", None))
                say(step_line(results[-1], store))
        for step in plan:
            lock.set_step(step.name)
            log.append("step_start", step=step.name, kind=step.spec.kind)
            result = execute(step, context, results)
            log.append("step_end", step=step.name, status=result.status, reason=result.reason, exit_code=result.exit_code)
            results.append(result)
            say(step_line(result, store))
        lock.set_step("doctor")
        after = doctor_summary(lab_checks(store))
        failed = [result for result in results if result.status == FAILED]
        incomplete = [result for result in results if result.status in {NEEDS_INPUT, REFUSED}]
        code = EXIT_PARTIAL if failed or incomplete else EXIT_OK
        log.append(
            "run_end", status="partial_failure" if code else "ok", exit_code=code,
            ok=sum(result.status == OK for result in results), failed=len(failed), step="doctor",
        )
    counts = {status: sum(result.status == status for result in results) for status in STATUSES}
    data = {
        "root": str(store.root),
        "yes": bool(args.yes),
        "only": only,
        "interactive": context.interactive,
        "steps": [result.public() for result in results],
        "counts": counts,
        "doctor_before": doctor_summary(before),
        "doctor_after": after,
    }
    tally = ", ".join(f"{count} {status}" for status, count in counts.items() if count) or "nothing to do"
    lines = [f"DOCTOR  {after['summary']}"]
    if after["setup_steps"]:
        lines.append(f"        {len(after['setup_steps'])} setup step{'s' if len(after['setup_steps']) != 1 else ''} still listed by `traceback lab doctor`")
    if failed:
        for result in failed:
            lines.append(f"FAILED  {result.name}  {show(result.reason, store)}")
            lines.extend(f"  | {line}" for line in result.tail)
        first = failed[0]
        problem = LabProblem(
            "TBX-LAB-032",
            cause=(
                f"{len(failed)} of {len(results)} setup step(s) failed; first: {first.name} ({first.reason}); "
                f"{tally}"
            ),
            fix=(
                "Read that step's output tail above, fix its cause, then rerun "
                "`traceback lab setup --root <ROOT>`; steps that ended ok are satisfied and not rerun"
            ),
            data={**data, "failed": [result.name for result in failed]},
        )
        payload = problem_payload("lab setup", problem, root=str(store.root))
        return problem.exit_code, _with_human(payload, [*lines, *problem_lines(problem)], replace_data=True)
    if incomplete:
        summary = f"setup incomplete: {tally}"
        if any(result.status == NEEDS_INPUT for result in incomplete):
            summary += "; supply the printed template paths"
        if any(result.status == REFUSED for result in incomplete):
            summary += "; rerun with --yes or confirm at a TTY"
        payload = _local_envelope(_result("lab setup", "partial_failure", summary, data=data))
    else:
        payload = _local_envelope(_result("lab setup", "ok", f"setup complete: {tally}", data=data))
    return code, _with_human(payload, lines, replace_data=True)


__all__ = [
    "CATALOGUE", "FAILED", "FLAGS", "KNOWN_STEPS", "MANUAL_STEPS", "NEEDS_INPUT", "OK", "REFUSED", "SKIPPED",
    "STATUSES", "SetupContext", "StepFailed", "StepResult", "StepSpec", "add_setup_parser", "execute",
    "plan_steps", "run_signed", "setup_command", "show", "step_line", "step_names",
]
