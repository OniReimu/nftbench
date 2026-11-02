"""Ask one OpenRouter model for real_code task mechanisms, using data/real_code/proposals/prompt.md."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from propose_openrouter import ROOT, SYSTEM, CLIFailure, OpenRouterBackend, SpendGuard

OUT = ROOT / "data" / "real_code" / "proposals"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--spend-cap-usd", type=float, required=True)
    args = parser.parse_args()
    name = args.model.split("/")[1]
    out = OUT / f"{name}.md"
    if out.exists():
        print(f"{name}: already there")
        return 0
    events = OUT / "events" / name
    events.mkdir(parents=True, exist_ok=True)
    guard = SpendGuard(args.spend_cap_usd, events)
    backend = OpenRouterBackend(args.model, guard=guard, events_path=events / "events.jsonl", max_tokens=64000)
    try:
        invocation = backend.invoke(SYSTEM, (OUT / "prompt.md").read_text())
    except CLIFailure as failure:
        print(f"{name}: FAILED {failure.kind}", flush=True)
        return 1
    out.write_text(invocation.text)
    usage = invocation.usage["usage"]
    print(f"{name}: in {usage.get('prompt_tokens')} out {usage.get('completion_tokens')} cost {usage.get('cost')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
