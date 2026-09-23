from __future__ import annotations

import os
import tempfile

from pathlib import Path

from eth_account import Account

ENVIRONMENT_PATH = Path(".env")


def main() -> None:
    buyer = Account.create()
    seller = Account.create()
    values = {
        "EVM_PRIVATE_KEY": f"0x{buyer.key.hex()}",
        "X402_BUYER_ADDRESS": buyer.address,
        "X402_SELLER_PRIVATE_KEY": f"0x{seller.key.hex()}",
        "X402_SELLER_ADDRESS": seller.address,
        "X402_PAY_TO": seller.address,
        "X402_NETWORK": "eip155:84532",
        "X402_FACILITATOR_URL": "https://x402.org/facilitator",
    }

    lines = (
        ENVIRONMENT_PATH.read_text(encoding="utf-8").splitlines()
        if ENVIRONMENT_PATH.exists()
        else []
    )
    updated: list[str] = []
    remaining = dict(values)
    for line in lines:
        name, separator, _ = line.partition("=")
        if separator and name in values:
            if name in remaining:
                updated.append(f"{name}={remaining.pop(name)}")
            continue
        updated.append(line)
    updated.extend(f"{name}={value}" for name, value in remaining.items())

    descriptor, temporary_name = tempfile.mkstemp(
        dir=ENVIRONMENT_PATH.parent, prefix=".env.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(updated) + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, ENVIRONMENT_PATH)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise

    print(f"Buyer:  {buyer.address}")
    print(f"Seller: {seller.address}")
    print(f"Wallet configuration written to {ENVIRONMENT_PATH}")


if __name__ == "__main__":
    main()
