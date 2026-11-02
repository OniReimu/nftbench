"""Shared constants, deterministic address helpers, and mechanism signature calculation."""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import yaml

from ..chain import ACCOUNTS, CHAIN_ID, GENESIS_TIMESTAMP, REPO_ROOT

GENERATED_DIR = REPO_ROOT / "tasks" / "generated"

DEPLOYER_NONCE_0 = "0x5fbdb2315678afecb367f032d93f642f64180aa3"
DEPLOYER_NONCE_1 = "0xe7f1725e7734ce288f8367e1bb143e90bb3f0512"
DEPLOYER_NONCE_2 = "0x9fe46736679d2d9a65f0992f2272de9f3c7fa6e0"
REGISTRY_ADDRESS = "0x000000006551c19487814612e58FE06813775758"
REGISTRY_FACTORY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
REGISTRY_DEPLOY_DATA = (
    "0x0000000000000000000000000000000000000000fd8eb4e1dca713016c518e31"
    "608060405234801561001057600080fd5b5061023b806100206000396000f3fe"
    "608060405234801561001057600080fd5b50600436106100365760003560e01c"
    "8063246a00211461003b5780638a54c52f1461006a575b600080fd5b61004e61"
    "00493660046101b7565b61007d565b6040516001600160a01b03909116815260"
    "200160405180910390f35b61004e6100783660046101b7565b6100e1565b6000"
    "60806024608c376e5af43d82803e903d91602b57fd5bf3606c5285605d52733d"
    "60ad80600a3d3981f3363d3d373d3d3d363d7360495260ff60005360b7605520"
    "6035523060601b60015284601552605560002060601b60601c60005260206000"
    "f35b600060806024608c376e5af43d82803e903d91602b57fd5bf3606c528560"
    "5d52733d60ad80600a3d3981f3363d3d373d3d3d363d7360495260ff60005360"
    "b76055206035523060601b600152846015526055600020803b61018b578560b7"
    "60556000f580610157576320188a596000526004601cfd5b80606c5250828488"
    "7f79f19b3655ee38b1ce526556b7731a20c8f218fbda4a3990b6cc4172fdf887"
    "226060606ca46020606cf35b8060601b60601c60005260206000f35b80356001"
    "600160a01b03811681146101b257600080fd5b919050565b6000806000806000"
    "60a086880312156101cf57600080fd5b6101d88661019b565b94506020860135"
    "9350604086013592506101f46060870161019b565b9497939650919460800135"
    "9291505056fea2646970667358221220ea2fe53af507453c64dd7c1db05549fa"
    "47a298dfb825d6d11e1689856135f16764736f6c63430008110033"
)

ZERO_ADDRESS = "0x" + "00" * 20
SALT_0 = "0x" + "00" * 32
SALT_1 = "0x" + "00" * 31 + "01"

DEFAULT_BUDGET = {
    "max_tool_calls": 40,
    "max_transactions": 6,
    "max_gas_total": 3_000_000,
    "max_gas_per_tx": 1_000_000,
}

ROLE_LOWER_ADDR = {role: account.address.lower() for role, account in ACCOUNTS.items()}

SIGNATURE_KINDS = ("signature_exists", "no_signature_except", "signature_used")


def participants_dict(names: tuple[str, ...] = ("Alice", "Bob", "Carol")) -> dict[str, str]:
    return {name: ACCOUNTS[name.lower()].address for name in names}


def registry_deploy_step() -> dict:
    return {
        "as": "registry",
        "signer": "deployer",
        "raw_to": REGISTRY_FACTORY,
        "data": REGISTRY_DEPLOY_DATA,
        "expect_address": REGISTRY_ADDRESS,
        "artifact": "ERC6551Registry",
    }


def iter_states(template) -> list[dict]:
    """Deterministically enumerate all valid state assignments from `template.STATE_VARS`."""
    keys = list(template.STATE_VARS.keys())
    domains = [template.STATE_VARS[k] for k in keys]
    valid_fn = getattr(template, "valid_state", lambda _s: True)
    states = []
    for values in itertools.product(*domains):
        state = dict(zip(keys, values))
        if valid_fn(state):
            states.append(state)
    sort_key = getattr(template, "state_sort_key", None)
    if sort_key is not None:
        states.sort(key=sort_key)
    return states


def state_neighbour_pairs(
    state_vars: dict[str, tuple],
    valid_state=lambda _s: True,
    pair_types: dict[str, str] | None = None,
) -> list[tuple[dict, dict, str, str]]:
    """Declare every single-variable neighbour among valid states as a pair."""
    keys = list(state_vars.keys())
    domains = [state_vars[k] for k in keys]
    states = [
        dict(zip(keys, values))
        for values in itertools.product(*domains)
        if valid_state(dict(zip(keys, values)))
    ]
    types_by_var = pair_types or {}
    pairs: list[tuple[dict, dict, str, str]] = []
    for idx, state_a in enumerate(states):
        for state_b in states[idx + 1 :]:
            changed = [k for k in keys if state_a[k] != state_b[k]]
            if len(changed) == 1:
                var = changed[0]
                pairs.append((state_a, state_b, types_by_var.get(var, "state"), var))
    return pairs


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120)


def is_signing_step(step: dict) -> bool:
    """True for a reference step `{sign: sign_typed_data | sign_message, signer, arguments, save_as}`."""
    return "sign" in step and "terminal" not in step


def is_transaction_step(step: dict) -> bool:
    return "terminal" not in step and "sign" not in step


def signing_primary_type(step: dict) -> str:
    """Primary type of a signing step: the EIP-712 primaryType, or `personal_message` for sign_message."""
    if step["sign"] == "sign_message":
        return "personal_message"
    typed = step.get("arguments", {}).get("typed_data", {})
    return typed.get("primaryType") or typed.get("primary_type") or ""


def signature_refs(value) -> list[str]:
    """Names of every `{signature: name}` reference nested in `value`, in order."""
    if isinstance(value, dict):
        if set(value) == {"signature"}:
            return [value["signature"]]
        return [name for item in value.values() for name in signature_refs(item)]
    if isinstance(value, list):
        return [name for item in value for name in signature_refs(item)]
    return []


def signing_reference_errors(oracle: dict, public: dict) -> list[str]:
    """Static structure errors of a reference trajectory's signing steps (empty for a signature-free one)."""
    steps = oracle.get("reference_trajectory", [])
    signing = [s for s in steps if is_signing_step(s)]
    refs_anywhere = [n for s in steps if "terminal" not in s for n in signature_refs(s.get("args", s.get("arguments", [])))]
    if not signing and not refs_anywhere:
        return []
    errors: list[str] = []
    if signing and not public.get("signing"):
        errors.append("V2_signing_not_enabled: reference signs but public.yaml has no signing: true")
    saved: dict[str, bool] = {}
    for step in steps:
        if "terminal" in step:
            continue
        if is_signing_step(step):
            if step["sign"] not in ("sign_typed_data", "sign_message"):
                errors.append(f"V2_unknown_signing_tool: {step['sign']!r}")
            for name in signature_refs(step.get("arguments", {})):
                if name not in saved:
                    errors.append(f"V2_signature_undefined: {name!r} used before it is signed")
                else:
                    saved[name] = True
            name = step.get("save_as")
            if not name:
                errors.append("V2_signature_unnamed: signing step has no save_as")
            elif name in saved:
                errors.append(f"V2_signature_duplicate: {name!r} saved twice")
            else:
                saved[name] = False
        else:
            for name in signature_refs(step.get("args", [])):
                if name not in saved:
                    errors.append(f"V2_signature_undefined: {name!r} used before it is signed")
                else:
                    saved[name] = True
    for name, used in saved.items():
        if not used:
            errors.append(f"V2_signature_unused: {name!r} is signed but no later step uses it")
    return errors


def normalized_trajectory(oracle: dict) -> list[dict]:
    """Strip free-text terminal arguments so action comparison checks only executable/terminal choices."""
    out = []
    for step in oracle.get("reference_trajectory", []):
        if "terminal" in step:
            out.append({"terminal": step["terminal"]})
        elif is_signing_step(step):
            out.append({
                "sign": step["sign"],
                "signer": step["signer"],
                "arguments": step.get("arguments", {}),
                "save_as": step.get("save_as"),
            })
        else:
            out.append({
                "signer": step["signer"],
                "to": step["to"],
                "function": step["function"],
                "args": step.get("args", []),
            })
    return out


def compute_mechanism_signature(public: dict, setup: dict, oracle: dict, family: str, standards: list[str]) -> str:
    """Canonical signature invariant to renaming participant names, token IDs, amounts, and addresses.

    Two instances that differ only in participant names, token IDs, numeric amounts/durations,
    or addresses produce the exact same mechanism signature and are rejected by V6.
    """
    role_map: dict[str, str] = {}
    token_map: dict[int, str] = {}

    def map_role(role: str) -> str:
        key = role.lower().removeprefix("$")
        if key == "deployer":
            return "DEPLOYER"
        if key not in role_map:
            role_map[key] = f"R{len(role_map)}"
        return role_map[key]

    def map_token(tid: int) -> str:
        if tid not in token_map:
            token_map[tid] = f"T{len(token_map)}"
        return token_map[tid]

    # Seed role map with signers first so signer role identity is canonical.
    for signer_name in public.get("signers", []):
        map_role(signer_name)

    addr_to_role = {addr.lower(): name.lower() for name, addr in public.get("participants", {}).items()}
    for role, acc in ACCOUNTS.items():
        addr_to_role.setdefault(acc.address.lower(), role)

    sig_map: dict[str, str] = {}

    def canon_sig(name):
        if name is None:
            return None
        if name not in sig_map:
            sig_map[name] = f"SIG{len(sig_map)}"
        return sig_map[name]

    def canon_value(val, *, is_token_id: bool = False, is_timestamp: bool = False):
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            if is_token_id:
                return map_token(val)
            if is_timestamp:
                if val == 0:
                    return "TS_ZERO"
                return "TS_PAST" if val < GENESIS_TIMESTAMP + 120 else "TS_FUTURE"
            return "ZERO" if val == 0 else "POS_NUM"
        if isinstance(val, str):
            low = val.lower()
            if low == ZERO_ADDRESS:
                return "ZERO_ADDR"
            if low in addr_to_role:
                return map_role(addr_to_role[low])
            if val.startswith("$"):
                base, plus, _offset = val[1:].partition("+")
                if base in ("t0", "genesis"):
                    return "REL_FUTURE" if plus else "T0"
                if base == "chain_id":
                    return "CHAIN_ID"
                if base in ("alice", "bob", "carol", "dave", "deployer"):
                    return map_role(base)
                return f"SYM:{base}"
            if low.startswith("0x") and len(low) == 66:
                return "SALT_0" if int(low, 16) == 0 else "SALT_NONZERO"
            if low.startswith("0x") and len(low) == 42:
                return "ADDR"
            return val
        if isinstance(val, list):
            return [canon_value(v) for v in val]
        if isinstance(val, dict):
            if set(val) == {"signature"}:
                return {"signature": canon_sig(val["signature"])}
            if "call" in val:
                c = val["call"]
                return {"call": c["function"], "to": canon_value(c["contract"]), "args": canon_args(c["function"], c.get("args", []))}
            return {k: canon_value(v) for k, v in sorted(val.items())}
        return val

    def canon_args(fn_name: str, args: list) -> list:
        fn = fn_name.split("(")[0]
        if fn == "mint" and len(args) == 2:
            return [canon_value(args[0]), canon_value(args[1], is_token_id=True)]
        if fn == "mint" and len(args) >= 3:
            return [canon_value(args[0]), canon_value(args[1], is_token_id=True), canon_value(args[2])] + [
                canon_value(a) for a in args[3:]
            ]
        if fn == "createUserRecord" and len(args) == 5:
            return [
                canon_value(args[0]),
                canon_value(args[1]),
                canon_value(args[2], is_token_id=True),
                canon_value(args[3]),
                canon_value(args[4], is_timestamp=isinstance(args[4], int)),
            ]
        if fn == "deleteUserRecord" and len(args) == 1:
            return [canon_value(args[0])]
        if fn in ("usableBalanceOf", "frozenBalanceOf") and len(args) == 2:
            return [canon_value(args[0]), canon_value(args[1], is_token_id=True)]
        if fn == "userRecordOf" and len(args) == 1:
            return [canon_value(args[0])]
        if fn == "safeBatchTransferFrom" and len(args) >= 4:
            return [
                canon_value(args[0]),
                canon_value(args[1]),
                [canon_value(i, is_token_id=True) for i in args[2]],
                [canon_value(v) for v in args[3]],
            ] + [canon_value(a) for a in args[4:]]
        if fn == "safeTransferFrom" and len(args) == 5:
            return [
                canon_value(args[0]),
                canon_value(args[1]),
                canon_value(args[2], is_token_id=True),
                canon_value(args[3]),
                canon_value(args[4]),
            ]
        if fn == "setLocked" and len(args) == 2:
            return [canon_value(args[0], is_token_id=True), canon_value(args[1])]
        if fn in ("locked", "ownerOf", "getApproved", "userOf", "userExpires") and len(args) == 1:
            return [canon_value(args[0], is_token_id=True)]
        if fn == "setUser" and len(args) == 3:
            return [
                canon_value(args[0], is_token_id=True),
                canon_value(args[1]),
                canon_value(args[2], is_timestamp=isinstance(args[2], int)),
            ]
        if fn == "approve" and len(args) == 2:
            # ERC-721 approve(to, tokenId) vs ERC-20 approve(spender, amount)
            return [canon_value(args[0]), canon_value(args[1], is_token_id=True)]
        if fn in ("transferFrom", "safeTransferFrom") and len(args) >= 3:
            return [canon_value(args[0]), canon_value(args[1]), canon_value(args[2], is_token_id=True)]
        if fn == "createAccount" and len(args) == 5:
            return [
                canon_value(args[0]),
                canon_value(args[1]),
                canon_value(args[2]),
                canon_value(args[3]),
                canon_value(args[4], is_token_id=True),
            ]
        if fn == "lock" and len(args) == 1:
            return [canon_value(args[0], is_timestamp=isinstance(args[0], int))]
        return [canon_value(a) for a in args]

    canon_setup = []
    for step in setup.get("steps", []):
        if "deploy" in step:
            canon_setup.append(("deploy", step["as"], step["deploy"], canon_args(step["deploy"], step.get("args", []))))
        elif "raw_to" in step:
            canon_setup.append(("raw_deploy", step["as"], step["artifact"]))
        else:
            canon_setup.append((
                "call",
                map_role(step["signer"]),
                canon_value(step["to"]),
                step["function"].split("(")[0],
                canon_args(step["function"], step.get("args", [])),
                step.get("as"),
            ))

    canon_traj = []
    for step in oracle.get("reference_trajectory", []):
        if "terminal" in step:
            canon_traj.append(("terminal", step["terminal"]))
        elif is_signing_step(step):
            canon_traj.append((
                "sign",
                step["sign"],
                signing_primary_type(step),
                canon_value(step["signer"]),
                canon_sig(step.get("save_as")),
            ))
        else:
            canon_traj.append((
                "tx",
                canon_value(step["signer"]),
                canon_value(step["to"]),
                step["function"].split("(")[0],
                canon_args(step["function"], step.get("args", [])),
            ))

    def canon_goal(c: dict):
        if c["kind"] in SIGNATURE_KINDS:
            return (c["kind"], canon_value({k: v for k, v in c.items() if k not in ("id", "kind")}))
        return (
            c["kind"],
            canon_value(c["contract"]),
            c.get("function"),
            canon_args(c.get("function", ""), c.get("args", [])),
            canon_value(c.get("equals")),
            canon_value(c.get("delta")),
            canon_value(c.get("center")),
            canon_value(c.get("owner")),
        )

    canon_goals = [canon_goal(c) for c in oracle.get("goal", [])]

    canon_invariants = []
    for inv in oracle.get("invariants", []):
        kind = inv["kind"]
        if kind in SIGNATURE_KINDS:
            canon_invariants.append((kind, canon_value({k: v for k, v in inv.items() if k not in ("id", "kind")})))
        elif kind in ("call_equals", "call_delta", "call_within"):
            canon_invariants.append((
                kind,
                canon_value(inv["contract"]),
                inv["function"],
                canon_args(inv["function"], inv.get("args", [])),
                canon_value(inv.get("equals")),
                canon_value(inv.get("delta")),
                canon_value(inv.get("center")),
            ))
        elif kind in ("no_movement_except", "no_approvals_granted", "no_rights_change_except"):
            canon_invariants.append((kind, canon_value(inv.get("allow", []))))
        else:
            canon_invariants.append((kind,))

    canon_faults = []
    for f in setup.get("faults", []):
        cf = dict(f)
        cf.setdefault("repeat", 1)
        if "signer" in f:
            cf["signer"] = canon_value(f["signer"])
        if "to" in f:
            cf["to"] = canon_value(f["to"])
        if "function" in f:
            cf["function"] = f["function"].split("(")[0]
            cf["args"] = canon_args(f["function"], f.get("args", []))
        canon_faults.append(sorted(cf.items()))

    payload = {
        "family": family,
        "standards": sorted(standards),
        "signers": [map_role(s) for s in public.get("signers", [])],
        "contracts": [(c["ref"], c["artifact"]) for c in public.get("contracts", [])],
        "setup": canon_setup,
        "label": oracle.get("label"),
        "trajectory": canon_traj,
        "goals": canon_goals,
        "invariants": canon_invariants,
    }
    if canon_faults:
        payload["faults"] = canon_faults
    if public.get("signing"):
        payload["signing"] = True
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
