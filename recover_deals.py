#!/usr/bin/env python3
"""
RECOVERY SCRIPT — Run this on ANY computer if VPS is lost.
Recovers deal escrow funds from the 12-word seed phrase.

Usage:
  1. pip install bip-utils tronpy
  2. python3 recover_deals.py

You will be asked for:
  - Your 12-word seed phrase
  - Deal indexes (from your backup of deal_states.json)

Each index gives you the private key + address.
Import the private key into TronLink to access funds.
"""
import sys

try:
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
    from tronpy.keys import PrivateKey
except ImportError:
    print("Install: pip install bip-utils tronpy")
    sys.exit(1)


def main():
    print("=" * 50)
    print("DEAL FUNDS RECOVERY")
    print("=" * 50)
    seed_phrase = input("\n12-word seed phrase: ").strip()
    if len(seed_phrase.split()) not in (12, 24):
        print("ERROR: Must be 12 or 24 words!")
        sys.exit(1)

    seed = Bip39SeedGenerator(seed_phrase).Generate()
    ctx = Bip44.FromSeed(seed, Bip44Coins.TRON).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)

    print("\nEnter deal HD indexes (one per line, empty to finish):")
    print("Tip: indexes are in your deal_states.json backup as 'tron_hd_index'")
    while True:
        idx_str = input("Index: ").strip()
        if not idx_str:
            break
        try:
            idx = int(idx_str)
        except ValueError:
            print("  Invalid number, try again.")
            continue
        node = ctx.AddressIndex(idx)
        address = node.PublicKey().ToAddress()
        priv_hex = node.PrivateKey().Raw().ToHex()
        print(f"\n  Index {idx}:")
        print(f"  Address: {address}")
        print(f"  Private Key: {priv_hex}")
        print(f"  → Import this private key into TronLink to access funds.")
        print(f"  → View: https://tronscan.org/#/address/{address}\n")

    print("Done. Keep private keys SECRET!")


if __name__ == "__main__":
    main()
