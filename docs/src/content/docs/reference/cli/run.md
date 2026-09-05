---
title: apm run
description: Execute a script or explicitly run a local contract on native Copilot.
sidebar:
  order: 12
---

Execute a shell command from `apm.yml` `scripts:`, npm-style, or explicitly
select a local contract with `--on copilot`.

:::caution[Experimental]
The `run` command surface is marked experimental. Flags and behavior may change before 1.0.
:::

## Synopsis

```bash
apm run [SCRIPT_NAME] [OPTIONS]
apm run CONTRACT --on copilot [--model MODEL] --allow-advisory [-v]
```

Without `--on`, omitting `SCRIPT_NAME` runs `start`; if absent, APM exits
non-zero and lists scripts. All legacy script and prompt behavior below
remains unchanged, even for script names ending in `.contract.md`.

## Description

Without `--on`, APM resolves `SCRIPT_NAME` against `scripts:` and runs the
matching shell command. It first compiles referenced `.prompt.md` files,
substituting `${input:name}` from `--param` and writing
`.apm/compiled/<name>.txt`.

If `SCRIPT_NAME` does not match a script, APM falls back to:

1. Auto-discovering a matching prompt file in `.apm/prompts/`, `.github/prompts/`, or the project root.
2. Auto-installing a virtual package reference (e.g., `owner/repo/path/to/prompt`) and re-running the discovery step.

If none of these resolve, the command exits non-zero with an error listing the available scripts.

## Options

| Option | Description |
|---|---|
| `-p, --param NAME=VALUE` | Set a parameter for prompt compilation. Repeat for multiple parameters. |
| `-v, --verbose` | Show detailed compilation and execution output. |
| `--on copilot` | Select contract mode; require a local `.contract.md`, without script/prompt discovery or installation. |
| `--model MODEL` | Request a native model; requires `--on`. |
| `--allow-advisory` | Accept native-host limits for this contract invocation; requires `--on`. |
| `--help` | Show help for the command. |

`--param` is rejected in contract mode. `--model` and `--allow-advisory` cannot
alter legacy scripts. There is no command-level `--json` flag.

## Examples

Define scripts in `apm.yml` (npm-style):

```yaml
name: hello-world-agent
version: 0.0.1

scripts:
  start: copilot -p hello-world.prompt.md --allow-all-tools
  claude: claude -p hello-world.prompt.md
  codex: codex exec --skip-git-repo-check hello-world.prompt.md
  llm: llm < hello-world.prompt.md

dependencies:
  apm:
    - dmeppiel/hello-world
```

Run the default `start` script:

```bash
apm run
```

Run a named script:

```bash
apm run claude
apm run codex
```

Pass parameters that get substituted into `${input:name}` placeholders inside `.prompt.md` files:

```bash
apm run start --param name="Alice"
apm run llm --param service=api --param environment=prod
```

List available scripts (no script defined and no `start`):

```bash
$ apm run
[x] No script specified and no 'start' script defined in apm.yml
[>] Available scripts:
   claude   claude -p hello-world.prompt.md
   codex    codex exec --skip-git-repo-check hello-world.prompt.md
   llm      llm < hello-world.prompt.md
```

## Argument forwarding

Scripts have no `--` argument passthrough. Use `--param NAME=VALUE` with
`${input:name}` in a `.prompt.md` file:

```markdown
Hello, ${input:name}. Today's target service is ${input:service}.
```

Then run:

```bash
apm run start --param name="Alice" --param service=api
```

Other parameterization belongs in the shell script body.

## Script exit codes

| Code | Meaning |
|---|---|
| `0` | Script executed successfully. |
| `1` | Script failed, was not found, or no `start` script is defined when invoked without arguments. |

## Contract execution

Run from the current directory containing `apm.yml`. Read the
[source format and read-only plan](../plan/) first, or follow the
[disposable fixture walkthrough](../../../consumer/run-contracts/).

### Native execution boundary

- **macOS/Linux only initially.** Windows execution refuses before launch.
  Native Copilot must be available and ready, with access to the requested
  model; Git and the check's host executables must be available.
- **No isolation.** Copilot and checks use your host identity and may access
  host files, network, and ambient credentials. Native extensions remain
  outside confinement.
- **Invocation-only consent.** `--allow-advisory` is required in both terminals
  and pipes. There is no prompt, saved/global consent, or policy override.
- **Positively no-policy projects only.** Start with independent local fixtures
  with no Git remote or configured policy requirement. Governed, unknown, or
  disabled policy discovery refuses, as does `APM_NO_SCRIPTS`. Never remove
  remotes or policy to evade a refusal.
- **Bounded waiting, not confinement.** The whole-attempt watchdog is 1,200
  seconds; each check gets at most 180 seconds, clipped to the remaining time.
  Cleanup attempts take at most six seconds and target the original process
  group. Escaped descendants are not confined.

Run preflight supervises `copilot mcp list --json` without a model, for at most
10 seconds within the attempt watchdog. APM uses Copilot's merged
User/Workspace/Plugin/Builtin inventory, not hardcoded config paths.
Only names are retained, not configuration values; each server is disabled through
per-invocation `--disable-mcp-server`. Missing, unsupported, malformed, timed-out,
or uncleanly stopped inventory refuses before the producer (`HALTED`, `22`).
Planning never runs this inventory.

The producer exposes only `view` and `apply_patch`, with an exact output-file
write grant and `--no-bash-env`; shell and URL tools are denied.

Package security remains separate: **built-in protection** automatically blocks
critical findings during `install`, `compile`, and `unpack`, with zero
configuration. **`apm audit`** is the explicit reporting (SARIF/JSON/markdown),
remediation (`--strip`), and standalone scanning (`--file`) tool. Contract checks
do not replace [either layer](../../../enterprise/security/).

### Captured inputs and checks

APM captures effective tracked working bytes, including dirty changes,
additions, deletions, and executable modes, plus selected untracked source,
needs, manifest, lock, and checker resources. Original Git `HEAD` alone is not
the baseline. A non-Git fixture supplies only explicit selections, not arbitrary
untracked or ignored files.

The producer uses an independent copy without the declared output. APM captures
the new output and gives each check a fresh baseline, the same frozen artifact,
and pre-generation `checks/**` resources. It does not use producer-edited helpers
or a previous check's workspace. If an artifact is a patch, the check applies it;
the engine **never pre-applies patches**.

Inputs and artifacts retain exact Unicode and raw bytes, including line endings.
Terminal sanitization does not rewrite them.

### Results and retained files

Artifacts stay in `.apm/runs/<run-id>/artifacts/`, never over the original project
output. The run directory contains `record.json` and private logs. Requested and
observed models are separate; unobserved model/usage values remain unknown.

Raw check exits `0`, `1`, and `2` mean pass, failure, and incomplete. Unknown
statuses, missing tools, signals, or invalid subject/resource identity mean
incomplete. A per-check timeout is incomplete unless the attempt watchdog
expires. Empty stderr does not negate exit `1`.

Apply these outcomes in order:

| Code | Outcome | Meaning |
|---|---|---|
| `22` | `HALTED` | Operational stop, cancellation, whole-attempt watchdog, capture-integrity, cleanup, or final-recording failure. |
| `20` | `REJECTED` | A substantive check exits `1`, including when another check is incomplete. |
| `21` | `UNPROVEN` | Missing artifact, required incomplete check, or unavailable consent/assurance, with no preceding outcome. |
| `0` | `VERIFIED` | Fresh artifact, all checks pass, accepted advisory controls, and completed record. |

A lingering check child yields `HALTED`, even if subsequent cleanup succeeds.

A missing artifact is not checked to manufacture rejection. Invalid source or
a missing executable is a preflight `HALTED` diagnostic; unsupported assurance
is `UNPROVEN`. CLI argument errors retain normal usage handling. Refusal before
admission does not claim a run exists. A nonterminal record means incomplete or
unknown, never permission to replay automatically.

Terminal output is one append-only phase stream with bounded, attributed,
untrusted activity and stderr. Controls are sanitized; private logs are
retrievable, not guaranteed secret-free or safe to publish. A stop request is
reported separately from observed original-group termination or uncertainty.

`VERIFIED` means the declared checks passed under native-advisory controls,
not prose correctness, merge approval, or protected attestation. There is no
dollar cap, cache, retry/resume, composition, or delivery facility.

## Related

- [`apm plan`](../plan/) -- inspect a contract without execution or durable writes.
- [`apm list`](../list/) -- show installed primitives and available scripts.
- [`apm preview`](../preview/) -- render the compiled command and prompt files without executing.
