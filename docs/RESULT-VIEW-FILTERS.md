# Result status, QC, and evidence filters

`evidence_inspector.result_view` is the pure E06 presentation-data boundary. It
does not render a framework, query a network service, execute a method, choose a
primary result, or change qualification. It converts bounded synthetic aggregate
records into a canonical, replayable view.

## Independent axes

The view carries these axes independently:

| Axis | Closed states |
| --- | --- |
| Execution | `complete`, `failed`, `not_run` |
| Information | `sufficient`, `insufficient`, `unknown` |
| Trust | `verified`, `revoked`, `unverified`, `unknown` |
| Qualification | `qualified`, `development_unqualified`, `unknown`, `not_assigned` |
| Display role | `provider_primary`, `research_baseline`, `research_challenger`, `disabled`, `not_assigned` |
| Compatibility | E05 `comparable`, `different_quantity`, `incompatible`, `unknown` |

No axis implies another. A qualified provider-primary result can still be
revoked, incomplete, or insufficient. Filtering one axis never upgrades or
rewrites any other axis. There is no automatic primary selection.

## Exact identity binding

Each `ResultViewSource` validates four exact identities against its immutable E05
record and compatibility decision:

1. compatibility decision, outcome, policy digest, and authority head;
2. method reference and method-definition digest;
3. registry version/digest, authority revision/head, and capability digest; and
4. result and bundle IDs plus their SHA-256 digests.

The compatibility replay binding also captures the record's execution,
information, and trust states. A decision created before any of those states
changes is stale and cannot be reused by the result view.

Normalized filters are sorted and deduplicated. Exact identity filters must be
present in the request's bounded source set. The request digest binds the sources,
filters, compatibility decisions, labels, and denominator ledgers. The view's
replay digest binds the normalized filters, visible rows, empty/ready state, and
reconciled counts. Replay recomputes the view and requires byte-equivalent contract
content.

## Denominators and attrition

Every visible row carries input, accepted, eligible, and displayed counts plus an
exact attrition reason for each transition. Observed values must reconcile:

```text
input = accepted + acceptance exclusions
accepted = eligible + eligibility exclusions
eligible = displayed + display exclusions
```

A count is explicitly `observed`, `missing`, or `withheld`. Missing and withheld
counts cannot carry a numeric value, including zero. Filters preserve the ledger
object unchanged; an empty filtered view reports zero visible rows but does not
rewrite a scientific count.

A `not_run` result requires every denominator stage and attrition count to be
`missing`. It remains displayable as an explicit state, but cannot carry observed
or withheld evidence counts.

## UI fixture states

`ResultViewFixture` supplies framework-independent, accessible states for:

- `loading`: label only, no stale result;
- `empty`: canonical view with no matching rows;
- `ready`: canonical view with one or more matching rows; and
- `error`: safe code and local retry message, with no result payload.

Labels are mandatory and status meaning is textual, not color-only. Fixture and
contract bounds prevent unbounded rows, identities, attrition reasons, or labels.

## Privacy and scope

Controlled labels reject path/URI syntax and reserved private-identifier terms.
Result contracts contain synthetic aggregate identities only, with no sequence,
person-level identifier, filesystem locator, external font, model call, or network
dependency. E06 does not add clinical interpretation, production deployment, or a
frontend framework commitment.
