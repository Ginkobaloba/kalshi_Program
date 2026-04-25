"""
Generate or recover Polymarket CLOB API credentials.

Polymarket uses two-tier auth:
  L1: EIP-712 signature with your Ethereum private key (proves wallet ownership)
  L2: HMAC with API key/secret/passphrase (used on every actual request)

This script does the L1 -> L2 handoff. Run it ONCE per wallet. The credentials
it produces let you call rate-limited read endpoints with higher quotas, and
in the future, place orders if/when you're on a compliant Polymarket product.

WARNING: keys are sensitive. Don't paste them in chats, screenshots, or git.

Setup:
  1. pip install py-clob-client
  2. Add to your .env:
       POLYMARKET_PRIVATE_KEY=0x...     # Ethereum private key (NEVER share)
  3. python scripts/polymarket_setup_api_keys.py

Output goes to stdout AND optionally writes into .env (with confirmation).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: python-dotenv not installed.")
    print("Run: pip install python-dotenv")
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("ERROR: py-clob-client not installed.")
    print("Run: pip install py-clob-client")
    sys.exit(1)


def main() -> int:
    load_dotenv()

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
        print("")
        print("Add this line to your .env file:")
        print("  POLYMARKET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY")
        print("")
        print("This is the private key to your Ethereum wallet that holds")
        print("USDC on Polygon. NEVER paste this anywhere except .env.")
        return 1

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    if len(private_key) != 66:
        print(f"ERROR: private key looks wrong (got {len(private_key)} chars, expected 66 incl. '0x')")
        return 1

    print("=" * 64)
    print("  POLYMARKET API KEY SETUP")
    print("=" * 64)
    print("")
    print("This will sign an EIP-712 message with your wallet to derive")
    print("CLOB API credentials. It does NOT send a transaction.")
    print("It does NOT cost gas.")
    print("")
    print("Compliance: these credentials are safe for READ endpoints from")
    print("US IPs. Using them for TRADING on Polymarket International from")
    print("a US IP violates Polymarket ToS - pm_bot's adapter enforces this.")
    print("")
    confirm = input("Continue? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("Aborted.")
        return 0

    print("")
    print("[1/2] Connecting to Polymarket CLOB...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,           # Polygon mainnet
        key=private_key,
    )
    try:
        print(f"   Wallet address: {client.get_address()}")
    except Exception:
        pass

    print("[2/2] Deriving API credentials (signing EIP-712 message)...")
    creds = client.create_or_derive_api_creds()

    print("")
    print("=" * 64)
    print("  CREDENTIALS")
    print("=" * 64)
    print(f"API Key:        {creds.api_key}")
    print(f"API Secret:     {creds.api_secret}")
    print(f"API Passphrase: {creds.api_passphrase}")
    print("=" * 64)
    print("")
    print("Treat these like passwords. Store them in .env now.")
    print("")

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        write = input(f"Append/update these in {env_path}? (yes/no): ").strip().lower()
        if write in ("yes", "y"):
            existing = env_path.read_text(encoding="utf-8")

            updates = {
                "POLYMARKET_API_KEY": creds.api_key,
                "POLYMARKET_API_SECRET": creds.api_secret,
                "POLYMARKET_API_PASSPHRASE": creds.api_passphrase,
            }

            new_lines: list[str] = []
            for line in existing.splitlines():
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates.pop(key)}")
                else:
                    new_lines.append(line)
            for k, v in updates.items():
                new_lines.append(f"{k}={v}")

            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            print(f"   Updated {env_path}")
            print("   Verify with `git status` that .env still shows as ignored.")
        else:
            print("Skipped writing. Save the credentials manually!")
    else:
        print(f"No .env at {env_path} - copy the credentials manually.")

    print("")
    print("Done. You can now use authenticated Polymarket reads.")
    print("Trading is still gated by the compliance flag in config.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
