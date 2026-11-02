"""The optional wallet JSON-RPC endpoint: off by default, allow-list only, same Episode as the native tools."""

from __future__ import annotations

import csv
import http.client
import json
import socket
from urllib.parse import urlsplit

import pytest

from helpers import ALICE, BOB, CAROL
from nftbench import evaluate, wallet_rpc
from nftbench.agents.llm import ScriptedBackend
from nftbench.chain import ACCOUNTS
from nftbench.runner import run_llm
from nftbench.tasks import Environment
from nftbench.tools import Episode
from nftbench.wallet_rpc import WalletRpc
from test_signing import permit_typed, write_task

KEYS = [bytes(account.key).hex() for account in ACCOUNTS.values()]
INFEASIBLE = json.dumps({"content": None, "tool_calls": [
    {"id": "c", "name": "declare_infeasible", "arguments": json.dumps({"reason": "x"})}]})


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    path = tmp_path_factory.mktemp("wallet_rpc_tasks")
    write_task(path, "S1", signing=True)
    write_task(path, "P1", signing=False)
    with (path / "manifest.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["instance_id", "label", "template"])
        writer.writerows([["S1", "feasible", "T"], ["P1", "feasible", "T"]])
    return path


@pytest.fixture(scope="module")
def env(root):
    with Environment("S1", root=root) as started:
        yield started


@pytest.fixture(scope="module")
def plain(root):
    with Environment("P1", root=root) as started:
        yield started


def post(url, payload, headers=None, path=None):
    """(status, raw body) of one POST to the endpoint, without any proxy."""
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        connection.request("POST", parts.path if path is None else path, body, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


BODIES: list[bytes] = []


def rpc(server, method, *params, id=1):
    status, raw = post(server.url, {"jsonrpc": "2.0", "id": id, "method": method, "params": list(params)})
    BODIES.append(raw)
    assert status == 200
    return json.loads(raw)


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_off_by_default_opens_no_port_and_outputs_are_identical(monkeypatch, root, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("a wallet endpoint was started without --wallet-rpc")

    monkeypatch.setattr(wallet_rpc.WalletRpc, "__init__", forbidden)
    monkeypatch.setattr(evaluate, "get_task_root", lambda tier: root)
    views = []

    class Recording(ScriptedBackend):
        def set_task_view(self, view):
            views.append(view)

    monkeypatch.setattr(evaluate, "load_backend_factory", lambda target: lambda: Recording([INFEASIBLE]))
    out = tmp_path / "off"
    assert evaluate.main(["--tier", "synthetic", "--backend", "x:y", "--instances", "P1", "--out", str(out)]) == 0
    assert views == []
    assert "wallet_rpc" not in json.loads((out / "run.json").read_text())
    with Environment("P1", root=root) as direct:
        direct_path = tmp_path / "direct.jsonl"
        report = run_llm(direct, ScriptedBackend([INFEASIBLE]), transcript_path=direct_path)
    assert (out / "P1" / "transcript.jsonl").read_bytes() == direct_path.read_bytes()
    assert json.loads((out / "P1" / "result.json").read_text())["report"] == json.loads(json.dumps(report))
    assert '"wallet_rpc"' not in (out / "P1" / "transcript.jsonl").read_text()


def test_evaluate_option_hands_the_url_to_the_backend_and_closes_it(monkeypatch, root, tmp_path):
    monkeypatch.setattr(evaluate, "get_task_root", lambda tier: root)
    seen = {}

    class Wallet(ScriptedBackend):
        def set_task_view(self, view):
            seen["view"] = view

        def complete(self, request):
            seen["request"] = request
            parts = urlsplit(seen["view"]["wallet_rpc_url"])
            seen["port"] = parts.port
            seen["accounts"] = json.loads(post(seen["view"]["wallet_rpc_url"], {"jsonrpc": "2.0", "id": 1, "method": "eth_accounts"})[1])
            return super().complete(request)

    monkeypatch.setattr(evaluate, "load_backend_factory", lambda target: lambda: Wallet([INFEASIBLE]))
    out = tmp_path / "on"
    assert evaluate.main(["--tier", "synthetic", "--backend", "x:y", "--instances", "P1", "--out", str(out), "--wallet-rpc"]) == 0
    view = seen["view"]
    assert view["wallet_rpc_url"].startswith("http://127.0.0.1:") and set(view) >= {"instruction", "participants", "signers", "budget"}
    assert view["wallet_rpc_url"] not in seen["request"] and view["wallet_rpc_url"].rsplit("/", 1)[1] not in (out / "P1" / "transcript.jsonl").read_text()
    assert {a.lower() for a in seen["accounts"]["result"]} == {ALICE.lower(), BOB.lower()}
    assert json.loads((out / "run.json").read_text())["wallet_rpc"] is True
    assert not listening(seen["port"])


def test_wallet_rpc_needs_a_backend(tmp_path, capsys):
    assert evaluate.main(["--tier", "synthetic", "--agent", "reference", "--out", str(tmp_path), "--wallet-rpc"]) == 1
    assert "requires --backend" in capsys.readouterr().err


def test_request_without_the_token_is_rejected(env):
    episode = Episode(env)
    with WalletRpc(episode) as server:
        call = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId"}
        assert post(server.url, call, path="/")[0] == 401
        assert post(server.url, call, path="/wrong-token")[0] == 401
        assert post(server.url, call, path="/" + server.url.rsplit("/", 1)[1] + "/extra")[0] == 401
        assert post(server.url, call, path="/", headers={"Authorization": "Bearer nope"})[0] == 401
        assert episode.trace.records == [] and episode.tool_calls == 0
        assert post(server.url, call)[0] == 200
        status, raw = post(server.url, call, path="/", headers={"Authorization": f"Bearer {server.url.rsplit('/', 1)[1]}"})
        assert status == 200 and json.loads(raw)["result"] == "0x7a69"
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("GET", "/" + server.url.rsplit("/", 1)[1])
        assert conn.getresponse().status == 405
        conn.close()


@pytest.mark.parametrize("method,params", [
    ("anvil_setBalance", [ALICE, "0x1"]),
    ("evm_mine", []),
    ("hardhat_impersonateAccount", [ALICE]),
    ("debug_traceTransaction", ["0x00"]),
    ("admin_nodeInfo", []),
    ("personal_unlockAccount", [ALICE, "pw"]),
    ("eth_sign", [ALICE, "0x00"]),
    ("eth_sendRawTransaction", ["0x00"]),
    ("no_such_method", []),
])
def test_blocked_methods_return_32601_and_are_recorded(env, method, params):
    episode = Episode(env)
    head = env.chain.block_number()
    with WalletRpc(episode) as server:
        reply = rpc(server, method, *params)
    assert reply["error"]["code"] == -32601 and "result" not in reply
    [record] = episode.trace.records
    assert record["kind"] == "refused" and record["tool"] == f"rpc:{method}" and record["reason"] == "method_not_allowed"
    assert episode.tool_calls == 0 and episode.trace.verify_chain() and env.chain.block_number() == head


def test_batch_runs_in_order_under_the_same_rules(env):
    episode = Episode(env)
    head = env.chain.block_number()
    with WalletRpc(episode) as server:
        batch = [
            {"jsonrpc": "2.0", "id": "a", "method": "eth_chainId"},
            {"jsonrpc": "2.0", "id": "b", "method": "evm_mine"},
            {"jsonrpc": "2.0", "id": "c", "method": "eth_getBalance", "params": [ALICE, "latest"]},
            {"jsonrpc": "2.0", "id": "d", "method": "anvil_reset"},
        ]
        status, raw = post(server.url, batch)
        BODIES.append(raw)
    replies = json.loads(raw)
    assert status == 200 and [r["id"] for r in replies] == ["a", "b", "c", "d"]
    assert replies[0]["result"] == "0x7a69" and replies[2]["result"].startswith("0x")
    assert replies[1]["error"]["code"] == replies[3]["error"]["code"] == -32601
    assert [(r["kind"], r.get("tool", r.get("method"))) for r in episode.trace.records] == [
        ("tool_call", "get_account_state"), ("rpc", "eth_chainId"), ("refused", "rpc:evm_mine"),
        ("tool_call", "get_account_state"), ("refused", "rpc:anvil_reset")]
    assert env.chain.block_number() == head


def test_allowed_read_and_submit_match_the_native_tools(env):
    native = Episode(env)
    tools = native.toolbox()
    assert tools.call("get_account_state", address=ALICE)["ok"]
    submitted = tools.call("submit_transaction", tx={"from": ALICE, "to": BOB, "value": 1})
    assert submitted["ok"]

    via_rpc = Episode(env)
    with WalletRpc(via_rpc) as server:
        balance = rpc(server, "eth_getBalance", ALICE, "latest")["result"]
        tx_hash = rpc(server, "eth_sendTransaction", {"from": ALICE, "to": BOB, "value": "0x1"})["result"]
        receipt = rpc(server, "eth_getTransactionReceipt", tx_hash)["result"]
        assert rpc(server, "eth_getTransactionReceipt", "0x" + "00" * 32)["result"] is None
    assert int(balance, 16) > 0 and tx_hash == submitted["tx_hash"]
    assert receipt["status"] == "0x1" and receipt["transactionHash"] == tx_hash
    assert [r["kind"] for r in via_rpc.trace.records][:2] == [r["kind"] for r in native.trace.records]
    assert via_rpc.trace.records[:2] == native.trace.records
    assert (via_rpc.tool_calls, via_rpc.tx_count, via_rpc.gas_used) == (native.tool_calls + 2, native.tx_count, native.gas_used)
    assert via_rpc.trace.verify_chain()


def test_rpc_submission_obeys_signer_list_and_budgets(env):
    budget = {**env.public["budget"], "max_transactions": 1, "max_tool_calls": 3}
    episode = Episode(env, budget=budget)
    with WalletRpc(episode) as server:
        refused = rpc(server, "eth_sendTransaction", {"from": CAROL, "to": BOB, "value": "0x1"})
        assert refused["error"]["message"] == "signer_not_available"
        assert "result" in rpc(server, "eth_sendTransaction", {"from": ALICE, "to": BOB, "value": "0x1"})
        assert rpc(server, "eth_sendTransaction", {"from": ALICE, "to": BOB, "value": "0x1"})["error"]["message"] == "budget_exhausted:max_transactions"
        assert rpc(server, "eth_blockNumber")["error"]["message"] == "budget_exhausted:max_tool_calls"
    assert [r["kind"] for r in episode.trace.records] == ["refused", "submitted", "refused", "refused"]
    assert episode.tx_count == 1


def test_estimate_call_and_logs(env):
    episode = Episode(env)
    with WalletRpc(episode) as server:
        tx = {"from": ALICE, "to": BOB, "value": "0x1"}
        assert int(rpc(server, "eth_estimateGas", tx)["result"], 16) == env.public["budget"]["max_gas_per_tx"]
        assert rpc(server, "eth_call", tx, "latest")["result"] == "0x"
        assert rpc(server, "eth_getBalance", ALICE, "0x5")["error"]["code"] == -32602
        assert rpc(server, "eth_getCode", ALICE)["result"] == "0x"
        assert rpc(server, "eth_getCode", env.symbols["nft"])["error"]["code"] == -32000
        assert int(rpc(server, "eth_blockNumber")["result"], 16) == env.chain.block_number()
        logs = rpc(server, "eth_getLogs", {"address": env.symbols["nft"], "fromBlock": "earliest", "toBlock": "latest"})["result"]
        assert isinstance(logs, list) and logs
        assert rpc(server, "eth_getLogs", {"blockHash": "0x00"})["error"]["code"] == -32602
    assert {r["tool"] for r in episode.trace.of_kind("simulated", "tool_call")} >= {"simulate_transaction", "get_account_state", "get_logs"}


def test_signing_through_rpc_matches_the_native_tools(env):
    typed = permit_typed(env)
    native = Episode(env)
    expected_typed = native.toolbox().call("sign_typed_data", signer=ALICE, typed_data=typed)["signature"]
    expected_message = native.toolbox().call("sign_message", signer=ALICE, message_hex="0x68656c6c6f")["signature"]

    via_rpc = Episode(env)
    with WalletRpc(via_rpc) as server:
        assert rpc(server, "eth_signTypedData_v4", ALICE, typed)["result"] == expected_typed
        assert rpc(server, "eth_signTypedData_v4", ALICE, json.dumps(typed))["result"] == expected_typed
        assert rpc(server, "personal_sign", "0x68656c6c6f", ALICE)["result"] == expected_message
        wrong_chain = rpc(server, "eth_signTypedData_v4", ALICE, permit_typed(env, chain_id=1))
        assert wrong_chain["error"]["message"].startswith("wrong_chain_id")
        assert rpc(server, "personal_sign", "0x00", CAROL)["error"]["message"] == "signer_not_available"
        assert rpc(server, "eth_signTypedData_v4", ALICE, "{not json")["error"]["code"] == -32602
    records = via_rpc.trace.of_kind("signing")
    assert [r["outcome"] for r in records] == ["signed", "signed", "signed", "refused", "refused"]
    assert records[0] == native.trace.records[0] and records[2]["message_hex"] == native.trace.records[1]["message_hex"]
    assert via_rpc.tool_calls == 5 and via_rpc.trace.verify_chain()


def test_signing_is_refused_in_a_non_signing_task(plain):
    episode = Episode(plain)
    typed = {"domain": {"chainId": 31337}, "types": {"T": []}, "primaryType": "T", "message": {}}
    with WalletRpc(episode) as server:
        assert rpc(server, "personal_sign", "0x00", ALICE)["error"]["message"] == "tool_not_available"
        assert rpc(server, "eth_signTypedData_v4", ALICE, typed)["error"]["message"] == "tool_not_available"
    assert not episode.trace.of_kind("signing")
    assert [(r["kind"], r["reason"]) for r in episode.trace.records] == [("refused", "tool_not_available")] * 2


def test_eth_accounts_lists_only_allowed_signers(env):
    with WalletRpc(Episode(env)) as server:
        for method in ("eth_accounts", "eth_requestAccounts"):
            accounts = rpc(server, method)["result"]
            assert {a.lower() for a in accounts} == {ALICE.lower(), BOB.lower()} and len(accounts) == 2
        assert rpc(server, "net_version")["result"] == "31337"


def test_malformed_bodies(env):
    with WalletRpc(Episode(env)) as server:
        assert json.loads(post(server.url, b"{")[1])["error"]["code"] == -32700
        assert json.loads(post(server.url, [])[1])["error"]["code"] == -32600
        assert json.loads(post(server.url, {"id": 1})[1])["error"]["code"] == -32600
        assert rpc(server, "eth_getBalance")["error"]["code"] == -32602


def test_port_closes_when_the_episode_ends(env):
    episode = Episode(env)
    server = WalletRpc(episode)
    port = server.port
    assert listening(port) and rpc(server, "eth_chainId")["result"] == "0x7a69"
    assert episode.toolbox().call("finish_task", summary="done")["ok"]
    assert rpc(server, "eth_blockNumber")["error"]["message"] == "episode_closed"
    server.close()
    assert not listening(port)
    server.close()


def test_no_key_appears_in_any_response_or_trace(env):
    episode = Episode(env)
    with WalletRpc(episode) as server:
        for method, params in [
            ("eth_accounts", []), ("eth_sign", [ALICE, "0x00"]), ("personal_sign", ["0x00", ALICE]),
            ("eth_signTypedData_v4", [ALICE, permit_typed(env)]), ("eth_signTypedData_v4", [CAROL, permit_typed(env)]),
            ("eth_sendTransaction", [{"from": ALICE, "to": BOB, "value": "0x1"}]), ("eth_getBalance", [ALICE]),
        ]:
            rpc(server, method, *params)
    haystack = (b"\n".join(BODIES).decode() + json.dumps(episode.trace.records, default=str)).lower()
    for key in KEYS:
        assert key not in haystack


def test_identity_calls_are_budgeted_traced_and_closed(env):
    episode = Episode(env)
    with WalletRpc(episode) as server:
        methods = ("eth_chainId", "net_version", "eth_accounts", "eth_requestAccounts")
        for method in methods:
            assert "result" in rpc(server, method)
        assert episode.tool_calls == 4
        assert [r["method"] for r in episode.trace.of_kind("rpc")] == list(methods)
        assert episode.toolbox().call("finish_task", summary="done")["ok"]
        before = len(episode.trace.records)
        for method in (*methods, "eth_blockNumber", "unknown_method"):
            assert rpc(server, method)["error"]["message"] == "episode_closed"
        assert len(episode.trace.records) == before


def test_identity_calls_respect_budget(env):
    episode = Episode(env, budget={**env.public["budget"], "max_tool_calls": 1})
    with WalletRpc(episode) as server:
        assert "result" in rpc(server, "eth_chainId")
        assert rpc(server, "net_version")["error"]["message"] == "budget_exhausted:max_tool_calls"
        assert episode.trace.of_kind("refused")[-1]["reason"] == "budget_exhausted:max_tool_calls"


def test_wallet_raw_permit_matches_function(env):
    from helpers import grade
    from nftbench import abi
    from test_signing import DEADLINE, TOKEN_ID, USED, clauses, oracle, sign_permit

    episode = Episode(env)
    tools = episode.toolbox()
    signature = sign_permit(tools, env)
    entry = abi.find_function(env.abi_for(env.symbols["nft"]), "permit")
    data = abi.encode_call(entry, [BOB, TOKEN_ID, DEADLINE, signature])
    with WalletRpc(episode) as server:
        assert "result" in rpc(server, "eth_sendTransaction", {"from": BOB, "to": env.symbols["nft"], "data": data})
    assert tools.call("finish_task", summary="done")["ok"]
    assert clauses(grade(env, episode, oracle([], [USED])), "trace_grader")["permit_used"]
