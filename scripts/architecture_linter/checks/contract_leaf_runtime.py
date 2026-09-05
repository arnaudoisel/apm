"""Keep leaf contracts outside legacy fallback and duplicate authorities."""

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, inventory_paths, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-leaf-runtime-owners"
PREFIX = "src/apm_cli/contracts/"
GUARDS = (
    "contracts-leaf-source",
    "contracts-leaf-subject",
    "contracts-leaf-process",
    "contracts-leaf-outcome",
)


def check_contract_owners(provider: FactsProvider) -> tuple[Violation, ...]:
    """Reject dangerous bypasses using the existing shared syntax facts."""
    findings: list[Violation] = []
    for path in inventory_paths(provider, prefixes=(PREFIX,)):
        if not path.endswith(".py"):
            continue
        facts, failures = checked_facts(provider, path, RULE_ID, require_python=True)
        findings.extend(failures)
        if failures:
            continue
        for imported in facts.imports:
            if (imported.module and imported.module.rsplit(".", 1)[-1] == "script_runner") or any(
                name in {"ScriptRunner", "PromptCompiler"} for name in imported.names
            ):
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        "Contracts must not enter legacy prompt discovery, installation or compilation.",
                        line=imported.line,
                    )
                )
        for call in facts.calls:
            name = call.qualname.rsplit(".", 1)[-1]
            message = None
            if name == "Popen" and path != PREFIX + "process.py":
                message = "Managed contract child creation belongs to contracts/process.py."
            elif name == "compute_file_hash":
                message = "Exact contract subjects must not use normalized deployment hashes."
            elif name in {"ScriptRunner", "PromptCompiler", "get_best_available_runtime"}:
                message = (
                    "Contracts require explicit source/runtime selection without legacy fallback."
                )
            if message:
                findings.append(violation(RULE_ID, path, message, line=call.line))
        for definition in facts.definitions:
            if (
                definition.name in {"normalize_check", "reduce_outcome"}
                and path != PREFIX + "records.py"
            ):
                findings.append(
                    violation(
                        RULE_ID,
                        path,
                        "Contract check normalization and outcome reduction belong to records.py.",
                        line=definition.line,
                    )
                )
    return tuple(findings)


RULES = (
    Rule(
        id=RULE_ID,
        group="contracts_tests",
        guard_ids=GUARDS,
        description="Leaf source, exact subjects, process supervision and outcomes keep canonical owners.",
        check=check_contract_owners,
    ),
)
