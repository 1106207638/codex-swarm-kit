"""Select current runtime models, without network, config writes or fallbacks.

stdin: {"source":"runtime-tool", "models":[{"id":"gpt-6-luna",
         "reasoning_efforts":["medium","high"]}, ...]}
Only a trusted controller may supply the CURRENT tool's advertised catalog.
This function validates shape, not the provenance of caller-provided metadata.
Use --role to resolve only a needed role; unrelated missing families do not block it.
"""
import argparse
import json
import re
import sys

ROLES = {
    "luna_worker": ("luna", "medium"),
    "luna_deep": ("luna", "high"),
    "luna_general": ("luna", "medium"),
    "terra_general": ("terra", "high"),
    "sol_architect": ("sol", "max"),
    "sol_planner": ("sol", "high"),
}
MODEL = re.compile(r"gpt-([0-9]+(?:\.[0-9]+)*)-(sol|luna|terra)", re.ASCII)


class ResolutionError(ValueError):
    pass


def version_key(value):
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _index(catalog, families):
    if not isinstance(catalog, dict) or catalog.get("source") != "runtime-tool":
        raise ResolutionError("Use the current tool's advertised catalog, not cache/web metadata")
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise ResolutionError("Runtime model list is missing or empty")
    by_family = {family: [] for family in families}
    seen = set()
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ResolutionError("Each model must have a string id")
        mid = item["id"]
        if mid in seen:
            raise ResolutionError("Duplicate model id: " + mid)
        seen.add(mid)
        match = MODEL.fullmatch(mid)
        related = [family for family in by_family if re.search(r"(?:^|-)" + family + r"(?:-|$)", mid)]
        if not related:
            continue
        visibility = item.get("visibility", "list")
        available = item.get("available", True)
        if visibility not in ("list", "hide") or not isinstance(available, bool):
            raise ResolutionError("Invalid availability metadata: " + mid)
        if visibility == "hide" or not available:
            continue
        if not match:
            raise ResolutionError("Unrecognized model naming in selected family; do not guess: " + mid)
        by_family[match[2]].append((version_key(match[1]), item))
    return by_family


def _select(candidates, family, effort):
    if not candidates:
        raise ResolutionError("No advertised stable numeric model for family: " + family)
    highest = max(version for version, _ in candidates)
    winners = [item for version, item in candidates if version == highest]
    if len(winners) != 1:
        raise ResolutionError("Ambiguous highest version in family: " + family)
    winner = winners[0]
    advertised_efforts = winner.get("reasoning_efforts")
    if not isinstance(advertised_efforts, list) or not all(isinstance(e, str) for e in advertised_efforts) or effort not in advertised_efforts:
        raise ResolutionError(f"Highest model {winner['id']} lacks confirmed effort {effort}; do not downgrade")
    return winner["id"]


def select_family(catalog, family, effort):
    """Use the same numeric-version rule for a main-thread target and its own catalog."""
    if family not in {"sol", "luna", "terra"}:
        raise ResolutionError("Unknown dynamic model family")
    return _select(_index(catalog, (family,))[family], family, effort)


def resolve(catalog, roles=None, *, efforts=None):
    selected_roles = list(ROLES) if roles is None else list(roles)
    if not selected_roles or any(role not in ROLES for role in selected_roles):
        raise ResolutionError("Unknown or empty role selection")
    if efforts is None:
        efforts = {}
    if not isinstance(efforts, dict):
        raise ResolutionError("Effort overrides must be a role-to-effort mapping")
    if any(role != "sol_planner" for role in efforts):
        raise ResolutionError("Only sol_planner effort may be overridden")
    if any(role not in selected_roles for role in efforts):
        raise ResolutionError("Effort override must target a selected role")
    if any(effort not in ("high", "max") for effort in efforts.values()):
        raise ResolutionError("sol_planner effort may only be high or max")
    by_family = _index(catalog, {ROLES[role][0] for role in selected_roles})
    result = {}
    for role in selected_roles:
        family, default_effort = ROLES[role]
        effort = efforts.get(role, default_effort)
        result[role] = {
            "family": family, "model": _select(by_family[family], family, effort), "reasoning_effort": effort,
            "agent_type": "default", "fork_turns": "none",
            "profile_file": role.replace("_", "-") + ".toml",
        }
    return {"source": "runtime-tool", "routes": result,
            "instruction": "Load complete role instructions and restrictions; supply full task packet. This is a selection, not invocation evidence."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", action="append", choices=ROLES)
    args = parser.parse_args()
    try:
        result = resolve(json.load(sys.stdin), args.role)
    except (ValueError, TypeError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
