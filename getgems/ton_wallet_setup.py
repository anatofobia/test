#!/usr/bin/env python3
"""
Утилита для безопасного (насколько возможно) сохранения seed-фразы TON без TonConnect.

Идея:
- seed-фраза НЕ хранится в .env в открытом виде
- вы генерируете/задаёте TON_MASTER_KEY (Fernet key) и шифруете seed → TON_WALLET_ENCRYPTED
- в .env сохраняете только TON_MASTER_KEY и TON_WALLET_ENCRYPTED

ВНИМАНИЕ:
Если TON_MASTER_KEY хранится на этом же сервере (в .env), то компрометация сервера = компрометация кошелька.
Используйте отдельный кошелёк под небольшие суммы.
"""

import os
import sys
import argparse

from cryptography.fernet import Fernet


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--print-master-key",
        action="store_true",
        help="Печатать TON_MASTER_KEY, который используется для шифрования (ОПАСНО: это секрет).",
    )
    parser.add_argument(
        "--force-generate-master-key",
        action="store_true",
        help="Всегда генерировать новый TON_MASTER_KEY (перешифрует mnemonic новым ключом).",
    )
    args = parser.parse_args()

    if sys.stdin.isatty():
        print("Вставьте seed-фразу (mnemonic) в stdin. Пример:")
        print("  cat mnemonic.txt | python3 ton_wallet_setup.py")
        print("или")
        print("  echo \"word1 ... word24\" | python3 ton_wallet_setup.py")
        return 2

    mnemonic = sys.stdin.read().strip()
    if not mnemonic:
        print("❌ Пустой ввод mnemonic", file=sys.stderr)
        return 2

    key = os.getenv("TON_MASTER_KEY", "").strip()
    if args.force_generate_master_key or not key:
        key = Fernet.generate_key().decode("utf-8")
        if not args.force_generate_master_key:
            print("⚠️ TON_MASTER_KEY не задан — сгенерирован новый ключ.")
        else:
            print("⚠️ Сгенерирован НОВЫЙ TON_MASTER_KEY (по флагу). Старый TON_WALLET_ENCRYPTED станет недействительным.")
        if args.print_master_key:
            print(f"TON_MASTER_KEY={key}")
        else:
            print("ℹ️ Чтобы вывести ключ, запустите с флагом: --print-master-key")

    f = Fernet(key.encode("utf-8"))
    token = f.encrypt(mnemonic.encode("utf-8")).decode("utf-8")

    print("\n✅ Готово. Сохраните в .env:")
    if args.print_master_key:
        print(f"TON_MASTER_KEY={key}")
    print(f"TON_WALLET_ENCRYPTED={token}")
    print("\n(Seed-фразу в .env НЕ кладите.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


