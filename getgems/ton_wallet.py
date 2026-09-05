import os
import base64
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp
from cryptography.fernet import Fernet, InvalidToken
from tonsdk.contract.wallet import Wallets, WalletVersionEnum
from tonsdk.utils import to_nano


class TonWalletError(Exception):
    pass


def _get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def get_master_key() -> bytes:
    """
    TON_MASTER_KEY должен быть urlsafe-base64 ключом Fernet (32 bytes).
    Пример генерации:
      python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    key = _get_env("TON_MASTER_KEY", "")
    if not key:
        raise TonWalletError("TON_MASTER_KEY_NOT_SET")
    try:
        # Fernet сам валидирует формат ключа
        Fernet(key.encode("utf-8"))
        return key.encode("utf-8")
    except Exception as e:
        raise TonWalletError(f"TON_MASTER_KEY_INVALID: {e}")

def _get_mnemonic_from_env_plain() -> str:
    """
    Простой (НЕбезопасный) режим: mnemonic хранится в .env как TON_WALLET_MNEMONIC.
    Формат: слова через пробел.
    """
    return _get_env("TON_WALLET_MNEMONIC", "")


def get_mnemonic() -> str:
    """
    Возвращает mnemonic:
    - если задан TON_WALLET_MNEMONIC → используем его (простой режим)
    - иначе пытаемся расшифровать TON_WALLET_ENCRYPTED через TON_MASTER_KEY
    """
    plain = _get_mnemonic_from_env_plain()
    if plain:
        return plain
    enc = _get_env("TON_WALLET_ENCRYPTED", "")
    return decrypt_mnemonic(enc)


def decrypt_mnemonic(encrypted: str) -> str:
    if not encrypted:
        raise TonWalletError("TON_WALLET_ENCRYPTED_NOT_SET")
    f = Fernet(get_master_key())
    try:
        plain = f.decrypt(encrypted.encode("utf-8"))
        return plain.decode("utf-8").strip()
    except InvalidToken:
        raise TonWalletError("TON_WALLET_ENCRYPTED_INVALID_TOKEN")
    except Exception as e:
        raise TonWalletError(f"TON_WALLET_DECRYPT_FAILED: {e}")


def encrypt_mnemonic(mnemonic: str) -> str:
    """
    Шифрует seed-фразу (mnemonic) в строку для хранения в .env как TON_WALLET_ENCRYPTED.
    """
    mnemonic = (mnemonic or "").strip()
    if not mnemonic:
        raise TonWalletError("MNEMONIC_EMPTY")
    f = Fernet(get_master_key())
    token = f.encrypt(mnemonic.encode("utf-8"))
    return token.decode("utf-8")


def _wallet_version() -> WalletVersionEnum:
    v = _get_env("TON_WALLET_VERSION", "v4r2").lower()
    mapping = {
        "v2r1": WalletVersionEnum.v2r1,
        "v2r2": WalletVersionEnum.v2r2,
        "v3r1": WalletVersionEnum.v3r1,
        "v3r2": WalletVersionEnum.v3r2,
        "v4r1": WalletVersionEnum.v4r1,
        "v4r2": WalletVersionEnum.v4r2,
    }
    return mapping.get(v, WalletVersionEnum.v4r2)

def _is_wallet_v5r1() -> bool:
    v = _get_env("TON_WALLET_VERSION", "v4r2").lower().replace("_", "").replace("-", "")
    return v in {"v5r1", "w5r1", "walletv5r1", "wallet5r1", "w5r01"}

def _wallet_id() -> Optional[int]:
    """
    wallet_id/subwallet влияет на адрес. Многие кошельки используют 698983191,
    но в некоторых случаях может отличаться.
    """
    raw = _get_env("TON_WALLET_ID", "")
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        raise TonWalletError("TON_WALLET_ID_INVALID")


def _wallet_kwargs() -> dict:
    kwargs = {}
    wid = _wallet_id()
    if wid is not None:
        kwargs["wallet_id"] = wid
    return kwargs


def _wallet_workchain() -> int:
    try:
        return int(_get_env("TON_WALLET_WORKCHAIN", "0"))
    except Exception:
        return 0


def _is_test_only() -> bool:
    return _get_env("TON_TESTNET", "0") in {"1", "true", "yes"}


def _is_bounceable() -> bool:
    return _get_env("TON_BOUNCEABLE", "1") not in {"0", "false", "no"}


def toncenter_base_url() -> str:
    # mainnet
    return _get_env("TONCENTER_BASE_URL", "https://toncenter.com/api/v2").rstrip("/")


def toncenter_api_key() -> str:
    return _get_env("TONCENTER_API_KEY", "")


@dataclass
class WalletIdentity:
    address: str
    address_raw: str


def load_wallet_identity() -> WalletIdentity:
    mnemonic = get_mnemonic()
    words = [w for w in mnemonic.split() if w.strip()]
    if len(words) not in {12, 15, 18, 21, 24}:
        raise TonWalletError("MNEMONIC_INVALID_WORD_COUNT")
    if _is_wallet_v5r1():
        # Wallet v5r1 (W5R1): адрес зависит от network_global_id и subwallet_number.
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key
        from pytoniq_core.boc.address import Address
        from pytoniq_core.tlb.account import StateInit
        from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5WalletID, WalletV5R1

        # -239 mainnet, -3 testnet
        network_global_id = int(_get_env("TON_NETWORK_GLOBAL_ID", "-239"))
        subwallet_number = int(_get_env("TON_SUBWALLET_NUMBER", "0"))
        wc = _wallet_workchain()

        _, priv = mnemonic_to_private_key(words)
        pub = private_key_to_public_key(priv)
        # Можно задать TON_WALLET_ID напрямую (в packed виде). Иначе вычисляем из network_global_id/subwallet_number.
        wid_raw = _get_env("TON_WALLET_ID", "")
        if wid_raw:
            try:
                wallet_id = int(wid_raw)
            except Exception:
                raise TonWalletError("TON_WALLET_ID_INVALID")
        else:
            wallet_id = WalletV5WalletID(network_global_id=network_global_id, workchain=wc, subwallet_number=subwallet_number, version=0).pack()
        data = WalletV5R1.create_data_cell(public_key=pub, wc=wc, wallet_id=wallet_id, is_signature_allowed=True)
        state_init = StateInit(code=WALLET_V5_R1_CODE, data=data)
        address = Address((wc, state_init.serialize().hash))
        addr = address.to_str(is_bounceable=_is_bounceable(), is_url_safe=True, is_test_only=_is_test_only())
        addr_raw = address.to_str(is_user_friendly=False)
        return WalletIdentity(address=addr, address_raw=addr_raw)

    # v2/v3/v4 (tonsdk)
    _, pub, priv, wallet = Wallets.from_mnemonics(words, version=_wallet_version(), workchain=_wallet_workchain(), **_wallet_kwargs())
    addr = wallet.address.to_string(is_user_friendly=True, is_url_safe=True, is_bounceable=_is_bounceable(), is_test_only=_is_test_only())
    addr_raw = wallet.address.to_string(is_user_friendly=False)
    return WalletIdentity(address=addr, address_raw=addr_raw)


class ToncenterV2:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or toncenter_base_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else toncenter_api_key()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            # toncenter поддерживает X-API-Key
            h["X-API-Key"] = self.api_key
        return h

    async def get_wallet_information(self, address: str) -> dict:
        url = f"{self.base_url}/getWalletInformation"
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.get(url, params={"address": address}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
                if not data.get("ok"):
                    raise TonWalletError(f"TONCENTER_getWalletInformation_FAILED: {data.get('error')}")
                return data["result"]

    async def get_address_balance(self, address: str) -> int:
        # toncenter возвращает balance в nanotons строкой
        info = await self.get_wallet_information(address)
        bal = info.get("balance", "0")
        try:
            return int(bal)
        except Exception:
            return 0

    async def send_boc(self, boc_b64: str) -> dict:
        url = f"{self.base_url}/sendBoc"
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(url, json={"boc": boc_b64}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
                if not data.get("ok"):
                    raise TonWalletError(f"TONCENTER_sendBoc_FAILED: {data.get('error')}")
                return data["result"]


def _load_wallet_contract() -> Tuple[list[str], bytes, bytes, object]:
    mnemonic = get_mnemonic()
    words = [w for w in mnemonic.split() if w.strip()]
    if len(words) not in {12, 15, 18, 21, 24}:
        raise TonWalletError("MNEMONIC_INVALID_WORD_COUNT")
    if _is_wallet_v5r1():
        # Для v5r1 tonsdk не используется
        return words, b"", b"", None
    return Wallets.from_mnemonics(words, version=_wallet_version(), workchain=_wallet_workchain(), **_wallet_kwargs())


async def ton_derive_addresses() -> list[dict]:
    """
    Возвращает варианты адресов для разных версий/кошелёк-id, чтобы понять какой совпадает с Tonkeeper.
    Никаких сетевых запросов не делает.
    """
    mnemonic = get_mnemonic()
    words = [w for w in mnemonic.split() if w.strip()]
    if len(words) not in {12, 15, 18, 21, 24}:
        raise TonWalletError("MNEMONIC_INVALID_WORD_COUNT")

    versions = [WalletVersionEnum.v4r2, WalletVersionEnum.v3r2, WalletVersionEnum.v4r1, WalletVersionEnum.v3r1]
    workchain = _wallet_workchain()
    wid_env = _wallet_id()
    wallet_ids = []
    if wid_env is not None:
        wallet_ids.append(wid_env)
    # популярные варианты
    for cand in [698983191, 0]:
        if cand not in wallet_ids:
            wallet_ids.append(cand)

    out = []
    for ver in versions:
        for wid in wallet_ids:
            try:
                _, pub, priv, wallet = Wallets.from_mnemonics(words, version=ver, workchain=workchain, wallet_id=wid)
                addr_b = wallet.address.to_string(is_user_friendly=True, is_url_safe=True, is_bounceable=True, is_test_only=_is_test_only())
                addr_nb = wallet.address.to_string(is_user_friendly=True, is_url_safe=True, is_bounceable=False, is_test_only=_is_test_only())
                addr_raw = wallet.address.to_string(is_user_friendly=False)
                out.append({
                    "version": str(ver.value),
                    "wallet_id": wid,
                    "workchain": workchain,
                    "bounceable": addr_b,
                    "non_bounceable": addr_nb,
                    "raw": addr_raw,
                })
            except Exception:
                continue

    # Добавим варианты для v5r1 (W5R1)
    try:
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key
        from pytoniq_core.boc.address import Address
        from pytoniq_core.tlb.account import StateInit
        from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5WalletID, WalletV5R1

        network_global_id = int(_get_env("TON_NETWORK_GLOBAL_ID", "-239"))
        subwallets = []
        # если явно задан TON_SUBWALLET_NUMBER — проверим его первым
        try:
            sw_env = int(_get_env("TON_SUBWALLET_NUMBER", "0"))
            subwallets.append(sw_env)
        except Exception:
            pass
        for cand in [0, 1, 2, 10, 100]:
            if cand not in subwallets:
                subwallets.append(cand)

        _, priv = mnemonic_to_private_key(words)
        pub = private_key_to_public_key(priv)
        wc = workchain

        for sw in subwallets[:8]:
            # packed wallet_id
            wallet_id = WalletV5WalletID(network_global_id=network_global_id, workchain=wc, subwallet_number=sw, version=0).pack()
            data = WalletV5R1.create_data_cell(public_key=pub, wc=wc, wallet_id=wallet_id, is_signature_allowed=True)
            state_init = StateInit(code=WALLET_V5_R1_CODE, data=data)
            address = Address((wc, state_init.serialize().hash))
            addr_b = address.to_str(is_bounceable=True, is_url_safe=True, is_test_only=_is_test_only())
            addr_nb = address.to_str(is_bounceable=False, is_url_safe=True, is_test_only=_is_test_only())
            addr_raw = address.to_str(is_user_friendly=False)
            out.append({
                "version": "v5r1",
                "wallet_id": wallet_id,
                "subwallet_number": sw,
                "network_global_id": network_global_id,
                "workchain": wc,
                "bounceable": addr_b,
                "non_bounceable": addr_nb,
                "raw": addr_raw,
            })
    except Exception:
        pass
    return out


async def ton_status() -> dict:
    ident = load_wallet_identity()
    client = ToncenterV2()
    bal = await client.get_address_balance(ident.address)
    return {"address": ident.address, "balance_nanoton": bal}


async def ton_deploy_wallet_if_needed() -> dict:
    """
    Деплой кошелька (инициализация) если account_state == 'uninitialized'.
    Требует, чтобы на адресе уже был баланс для оплаты комиссии.
    """
    client = ToncenterV2()
    ident = load_wallet_identity()
    addr = ident.address

    info = await client.get_wallet_information(addr)
    if info.get("account_state") != "uninitialized":
        return {"deployed": True, "already": True, "address": addr}

    if _is_wallet_v5r1():
        # Deploy v5r1: external message with state_init + body (empty transfer)
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key
        from pytoniq_core.crypto.signature import sign_message
        from pytoniq_core.boc import Builder, begin_cell, Cell
        from pytoniq_core.boc.address import Address
        from pytoniq_core.tlb.account import StateInit
        from pytoniq.contract.contract import Contract
        from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5WalletID, WalletV5R1

        mnemonic = get_mnemonic()
        words = [w for w in mnemonic.split() if w.strip()]
        _, priv = mnemonic_to_private_key(words)
        pub = private_key_to_public_key(priv)
        wc = _wallet_workchain()
        network_global_id = int(_get_env("TON_NETWORK_GLOBAL_ID", "-239"))
        subwallet_number = int(_get_env("TON_SUBWALLET_NUMBER", "0"))
        wallet_id = WalletV5WalletID(network_global_id=network_global_id, workchain=wc, subwallet_number=subwallet_number, version=0).pack()
        data = WalletV5R1.create_data_cell(public_key=pub, wc=wc, wallet_id=wallet_id, is_signature_allowed=True)
        state_init = StateInit(code=WALLET_V5_R1_CODE, data=data)
        address = Address((wc, state_init.serialize().hash))

        # body for deploy: raw_create_transfer_msg with seqno=0 and no actions
        op_code = 0x7369676e
        signing_message = begin_cell().store_uint(op_code, 32)
        signing_message.store_uint(wallet_id, 32)
        signing_message.store_uint(2**32 - 1, 32)  # seqno=0 deploy case
        signing_message.store_uint(0, 32)          # seqno
        signing_message.store_cell(WalletV5R1.pack_actions([]))
        signing_message = signing_message.end_cell()
        signature = sign_message(signing_message.hash, priv)
        body = Builder().store_cell(signing_message).store_bytes(signature).end_cell()

        ext_msg = Contract.create_external_msg(dest=address, state_init=state_init, body=body)
        boc = ext_msg.serialize().to_boc()
        boc_b64 = base64.b64encode(boc).decode("utf-8")
        result = await client.send_boc(boc_b64)
        return {"deployed": True, "already": False, "address": addr, "result": result}

    # v2/v3/v4 deploy (tonsdk)
    words, pub, priv, wallet = _load_wallet_contract()
    init = wallet.create_init_external_message()
    boc = init["message"].to_boc(False)
    boc_b64 = base64.b64encode(boc).decode("utf-8")
    result = await client.send_boc(boc_b64)
    return {"deployed": True, "already": False, "address": addr, "result": result}


async def ton_send(to_address: str, amount_ton: float, comment: str = "") -> dict:
    if not to_address:
        raise TonWalletError("TO_ADDRESS_EMPTY")
    if amount_ton <= 0:
        raise TonWalletError("AMOUNT_MUST_BE_POSITIVE")

    client = ToncenterV2()
    ident = load_wallet_identity()
    from_addr = ident.address
    info = await client.get_wallet_information(from_addr)
    seqno = int(info.get("seqno") or 0) if info.get("wallet") else 0
    amount_nano = int(to_nano(amount_ton, "ton"))

    if _is_wallet_v5r1():
        # Build external message for v5r1 and send via toncenter sendBoc
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key
        from pytoniq_core.crypto.signature import sign_message
        from pytoniq_core.boc import Builder, begin_cell, Cell
        from pytoniq_core.boc.address import Address
        from pytoniq.contract.contract import Contract
        from pytoniq_core.tlb.custom.wallet import WalletMessage
        from pytoniq.contract.wallets.wallet import Wallet as _W
        from pytoniq.contract.wallets.wallet_v5 import WalletV5WalletID, WalletV5R1

        mnemonic = get_mnemonic()
        words = [w for w in mnemonic.split() if w.strip()]
        _, priv = mnemonic_to_private_key(words)
        wc = _wallet_workchain()
        network_global_id = int(_get_env("TON_NETWORK_GLOBAL_ID", "-239"))
        subwallet_number = int(_get_env("TON_SUBWALLET_NUMBER", "0"))
        wallet_id = WalletV5WalletID(network_global_id=network_global_id, workchain=wc, subwallet_number=subwallet_number, version=0).pack()

        dest = Address(to_address)
        body_cell = Cell.empty()
        if comment:
            body_cell = Builder().store_uint(0, 32).store_snake_string(comment).end_cell()
        internal_msg = Contract.create_internal_msg(dest=dest, value=amount_nano, body=body_cell)
        wallet_msg = WalletMessage(send_mode=3, message=internal_msg)

        op_code = 0x7369676e
        signing_message = begin_cell().store_uint(op_code, 32)
        signing_message.store_uint(wallet_id, 32)
        if seqno == 0:
            signing_message.store_uint(2**32 - 1, 32)
        else:
            signing_message.store_uint(int(time.time()) + 60, 32)
        signing_message.store_uint(seqno, 32)
        signing_message.store_cell(WalletV5R1.pack_actions([wallet_msg]))
        signing_message = signing_message.end_cell()
        signature = sign_message(signing_message.hash, priv)
        body = Builder().store_cell(signing_message).store_bytes(signature).end_cell()

        ext_msg = Contract.create_external_msg(dest=Address(from_addr), body=body)
        boc = ext_msg.serialize().to_boc()
        boc_b64 = base64.b64encode(boc).decode("utf-8")
        result = await client.send_boc(boc_b64)
        return {"from": from_addr, "to": to_address, "amount_ton": amount_ton, "seqno": seqno, "result": result}

    # v2/v3/v4 via tonsdk
    words, pub, priv, wallet = _load_wallet_contract()
    tr = wallet.create_transfer_message(to_addr=to_address, amount=amount_nano, seqno=seqno, payload=comment or None)
    ext = wallet.create_external_message(tr["signing_message"], seqno=seqno)
    boc = ext["message"].to_boc(False)
    boc_b64 = base64.b64encode(boc).decode("utf-8")
    result = await client.send_boc(boc_b64)
    return {"from": from_addr, "to": to_address, "amount_ton": amount_ton, "seqno": seqno, "result": result}


