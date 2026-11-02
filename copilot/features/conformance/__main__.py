"""Run offline contract checks on demand; no schema download or PayPal call."""
from copilot import config


def main():
    if not config.feature_enabled("conformance"):
        print("Conformance is switched off. Set COPILOT_FEATURE_CONFORMANCE=true to run offline checks.")
        return 2
    from .suite import configured_run
    result, skipped = configured_run()
    for tier in skipped:
        print(tier + " unset or absent: optional tier skipped; synthetic subset tier ran.")
    print(result["sentence"])
    return 0 if all(not r["issues"] for r in result["mapped"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
