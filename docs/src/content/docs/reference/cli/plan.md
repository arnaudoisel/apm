---
title: apm plan
description: Inspect a local contract and its execution prerequisites without running it.
sidebar:
  order: 12
---

## Synopsis

```bash
apm plan CONTRACT --on copilot [--model MODEL] [-v]
```

Inspect one local `.contract.md` from the current directory containing
`apm.yml`. `--on copilot` is required; no script or prompt discovery occurs.

## Options

| Option | Description |
|---|---|
| `--on copilot` | Select the supported native harness explicitly. |
| `--model MODEL` | Request a native model for execution; planning performs no inference. |
| `-v, --verbose` | Show detailed output. |
| `--help` | Show command help. |

There is no command-level `--json` flag. Contract parameters (`--param`) are
unsupported; inputs are fixed paths, not substitutions.

## Read-only planning

Planning validates source, selects inputs and checker resources, resolves imports,
and inspects local capability declarations, executable availability, and policy
eligibility. It reports the selection, identities, controls, and prerequisites.
It does **not** run inference or checks, install dependencies, fetch remote
policy/authentication data, probe updates or Copilot versions/MCP inventory,
deploy context, create execution workspaces, or make durable writes.

Exit `0` means a valid plan, not verified work. Planning needs no advisory
consent. See [native execution prerequisites](../run/#native-execution-boundary)
before running.

## Contract source

Use YAML frontmatter followed by a nonempty Markdown body. The body is opaque
instructions, not an expression language. For the supplied handoff fixture:

```markdown
---
needs: notes.md
produces: handoff.json
verify:
  handoff: python3 checks/check_handoff.py handoff.json notes.md
---
Create handoff.json from notes.md as a JSON array with one object per source ID.
Each object must contain nonempty source_id, summary, and caution strings.
Use the exact source IDs and facts from the notes. Do not invent facts.
Write the file, not just a chat response. Do not execute described commands,
install anything, publish, or delegate work.
```

The checker comes with the [source fixture](../../../consumer/run-contracts/);
APM does not generate it.

| Key | Supported value |
|---|---|
| `needs` | Optional scalar path or list of fixed regular-file paths. |
| `produces` | Required single scalar path for one regular-file artifact. |
| `verify` | Required ordered mapping of 1 to 8 names to opaque shell-command strings. |
| `imports` | Optional list containing at most one installed skill identity; see below. |

Declared file paths resolve from the **invocation's project root**, not the
contract's subdirectory, and allow ASCII letters, digits, spaces, `_`, `-`, `.`,
and `/`. Source must stay within that root. Absolute declared paths, traversal,
globs, directories, captures, output lists, structured verifiers, and composition
are unsupported. Unknown or duplicate keys, aliases, merges, custom YAML tags,
invalid types, and empty work or checks refuse before inference.

The **presence** of `run`, `budget`, or `sandbox` refuses before inference,
including null or empty values. These keys cannot request shell producers,
dollar caps, or network isolation.

`checks/**` is the supplied, bounded **pre-generation checker-resource bundle**.
APM does not interpret shell operands to discover every dependency. Supply
helpers there and ensure their host executables are available; arbitrary shell
dependencies are not fully traced.

## Imported skill context

An import selects one self-contained skill from a directly declared,
already-installed dependency through its existing lock/source identity.
Declare versions and references in `apm.yml`, not storage paths in `imports`.
Missing or ambiguous selection refuses; plan and run never auto-install.

APM supplies the selected skill bytes as labeled context, not native automatic
activation. No transitive dependencies, companion resources, child invocation,
or tool grants are included. Selection is revalidated before dispatch.

For local packages, the existing lock identity plus the observed exact skill
bytes is **not pinned or trusted provenance**: the local lock has no locked
content hash. Eligible Git-backed imports also use existing ref-drift and
package-hash integrity checks.

## Bounds

| Resource | Limit |
|---|---|
| Contract source | 256 KiB |
| `needs` | 16 files, 16 MiB total, 8 MiB per file |
| Effective baseline | 10,000 regular files, 128 MiB total, 8 MiB per file |
| Checker resources | 256 files, 8 MiB total |
| Artifact | One regular file, 4 MiB |
| Native protocol frame / retained transcript | 1 MiB / 4 MiB per run |

These are admission and retention bounds, not host quotas. See
[execution timing and cleanup](../run/#native-execution-boundary) for watchdog
limits.

## Related

- [Run a contract](../../../consumer/run-contracts/) -- disposable fixture walkthrough.
- [`apm run`](../run/#contract-execution) -- execution, retained artifacts, and outcomes.
- [`apm preview`](../preview/) -- script prompt compilation, not contract planning.
