"""
Auto-redeemer for resolved Polymarket positions.

Design: fully non-blocking. check_and_redeem() returns instantly and fires a
background daemon thread to handle the 120s settle delay + on-chain execution.
Trading is never paused. Redeemed USDC is collected on the next bet cycle via
pop_redeemed().

Architecture:
  - FUNDER is a Gnosis Safe proxy (SIGNATURE_TYPE=2)
  - EOA (WALLET_ADDRESS) owns the Safe and signs execTransaction
  - Safe holds CTF tokens; Safe calls CTF.redeemPositions
  - Two execution paths: on-chain (needs POL) → relay (gasless, shared quota)
"""

import json
import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

POLYGON_RPC     = "https://polygon-bor-rpc.publicnode.com"
RELAY_URL       = "https://relayer-v2.polymarket.com"
SIGN_URL        = "https://builder-signing-server.vercel.app/sign"

CTF_ADDRESS     = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
HASH_ZERO       = b"\x00" * 32
ADDRESS_ZERO    = "0x0000000000000000000000000000000000000000"

SETTLE_DELAY    = 120    # seconds after resolution before redeeming (oracle buffer)
MIN_VALUE       = 0.50   # skip positions worth less than $0.50
WIN_THRESHOLD   = 0.99   # curPrice >= this = won
MIN_POL_FOR_GAS = 0.005  # EOA POL required for on-chain path

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
    }
]

SAFE_ABI = [
    {
        "name": "getTransactionHash",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "nonce",          "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "execTransaction",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures",     "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
    },
    {
        "name": "nonce",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ── Module-level state ────────────────────────────────────────────────────────

_lock = threading.Lock()
_redeemed_this_session: set = set()    # conditionIds already done
_pending_redeemed: float = 0.0         # USDC redeemed in background, not yet collected
_redemption_thread: threading.Thread | None = None


def pop_redeemed() -> float:
    """
    Collect USDC redeemed by background thread since last call.
    Called from the main trading loop to update bankroll.balance.
    """
    global _pending_redeemed
    with _lock:
        amount = _pending_redeemed
        _pending_redeemed = 0.0
    return amount


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_winning_positions(funder: str) -> list[dict]:
    try:
        resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={funder}&redeemable=true",
            timeout=10,
        )
        resp.raise_for_status()
        positions = resp.json()
        with _lock:
            already_done = set(_redeemed_this_session)
        return [
            p for p in positions
            if float(p.get("curPrice", 0)) >= WIN_THRESHOLD
            and float(p.get("currentValue", 0)) >= MIN_VALUE
            and p.get("conditionId") not in already_done
        ]
    except Exception as e:
        print(f"[redeemer] fetch failed: {e}")
        return []


def _encode_redeem_calldata(w3, condition_id_hex: str) -> str:
    ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    cid_bytes = bytes.fromhex(condition_id_hex.removeprefix("0x"))
    return ctf.encode_abi(
        abi_element_identifier="redeemPositions",
        args=[w3.to_checksum_address(USDC_ADDRESS), HASH_ZERO, cid_bytes, [1, 2]],
    )


def _pack_safe_signature(r: int, s: int, v: int) -> bytes:
    if v in (0, 1):
        v += 31
    elif v in (27, 28):
        v += 4
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])


def _sign_safe_tx(account, safe_contract, to: str, data: str, nonce: int):
    from eth_account.messages import encode_defunct
    tx_hash_bytes = safe_contract.functions.getTransactionHash(
        to, 0, data, 0, 0, 0, 0, ADDRESS_ZERO, ADDRESS_ZERO, nonce,
    ).call()
    return account.sign_message(encode_defunct(hexstr=tx_hash_bytes.hex()))


def _redeem_onchain(w3, account, funder: str, to: str, data: str) -> str:
    safe = w3.eth.contract(address=w3.to_checksum_address(funder), abi=SAFE_ABI)
    safe_nonce = safe.functions.nonce().call()
    signed = _sign_safe_tx(account, safe, to, data, safe_nonce)
    packed_sig = _pack_safe_signature(signed.r, signed.s, signed.v)

    tx = safe.functions.execTransaction(
        to, 0, data, 0, 0, 0, 0, ADDRESS_ZERO, ADDRESS_ZERO, packed_sig,
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address, "pending"),
        "gas":      300_000,
        "gasPrice": int(w3.eth.gas_price * 1.1),
        "chainId":  137,
    })

    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if receipt.status != 1:
        raise Exception(f"execTransaction reverted (tx={tx_hash.hex()})")
    return tx_hash.hex()


def _redeem_via_relay(w3, account, funder: str, eoa: str, to: str, data: str) -> str:
    from eth_account.messages import encode_defunct
    safe = w3.eth.contract(address=w3.to_checksum_address(funder), abi=SAFE_ABI)

    nonce_r = requests.get(f"{RELAY_URL}/nonce", params={"address": eoa, "type": "SAFE"}, timeout=10)
    nonce_r.raise_for_status()
    safe_nonce = int(nonce_r.json()["nonce"])

    signed = _sign_safe_tx(account, safe, to, data, safe_nonce)
    sig = signed.signature.hex()
    last = sig[-2:]
    if last in ("00", "1b"): sig = sig[:-2] + "1f"
    elif last in ("01", "1c"): sig = sig[:-2] + "20"

    body = {
        "data": data, "from": eoa, "metadata": "redeem",
        "nonce": str(safe_nonce), "proxyWallet": funder,
        "signature": "0x" + sig,
        "signatureParams": {
            "baseGas": "0", "gasPrice": "0", "gasToken": ADDRESS_ZERO,
            "operation": "0", "refundReceiver": ADDRESS_ZERO, "safeTxnGas": "0",
        },
        "to": to, "type": "SAFE",
    }
    body_str = json.dumps(body)
    headers_resp = requests.post(SIGN_URL, json={"method": "POST", "path": "/submit", "body": body_str}, timeout=10)
    headers_resp.raise_for_status()
    submit = requests.post(f"{RELAY_URL}/submit", headers=headers_resp.json(), data=body_str.encode(), timeout=30)
    submit.raise_for_status()
    return submit.json().get("transactionHash", "relay-ok")


# ── Background worker ─────────────────────────────────────────────────────────

def _bg_redeem_worker(funder: str, private_key: str, winning: list[dict]) -> None:
    """
    Runs in a daemon thread. Waits for oracle settlement, then redeems
    each winning position sequentially. Stores result in _pending_redeemed.
    """
    global _pending_redeemed
    import telegram_alerts as tg
    from web3 import Web3
    from eth_account import Account

    total_value = sum(float(p.get("currentValue", 0)) for p in winning)
    print(f"[redeemer] Background: waiting {SETTLE_DELAY}s to settle ${total_value:.2f}...")
    time.sleep(SETTLE_DELAY)

    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        if not w3.is_connected():
            raise Exception("Cannot connect to Polygon RPC")

        account = Account.from_key(private_key)
        eoa = account.address
        ctf = w3.to_checksum_address(CTF_ADDRESS)

        pol = w3.eth.get_balance(eoa) / 1e18
        use_onchain = pol >= MIN_POL_FOR_GAS
        print(f"[redeemer] POL={pol:.4f} — {'on-chain' if use_onchain else 'relay'}")

        redeemed = 0.0
        for pos in winning:
            cid   = pos["conditionId"]
            value = float(pos.get("currentValue", 0))
            title = pos.get("title", "")[:50]
            try:
                data = _encode_redeem_calldata(w3, cid)
                if use_onchain:
                    tx = _redeem_onchain(w3, account, funder, ctf, data)
                else:
                    tx = _redeem_via_relay(w3, account, funder, eoa, ctf, data)

                with _lock:
                    _redeemed_this_session.add(cid)
                redeemed += value
                print(f"[redeemer] ✅ ${value:.2f} {title}  tx={tx[:18]}…")

            except Exception as e:
                err = str(e)
                print(f"[redeemer] ❌ {title}: {err[:120]}")
                tg.send_message(f"❌ Redeem failed '{title[:40]}': {err[:200]}")
                # continue to next position — don't abort the whole batch

        if redeemed > 0:
            with _lock:
                _pending_redeemed += redeemed
            print(f"[redeemer] Done — ${redeemed:.2f} USDC queued for bankroll")
            tg.send_message(f"💰 Redeemed ${redeemed:.2f} USDC")

    except Exception as e:
        import telegram_alerts as tg
        err = str(e)
        print(f"[redeemer] Worker error: {err}")
        tg.send_message(f"⚠️ Redeemer worker failed: {err[:300]}")


# ── Public API ────────────────────────────────────────────────────────────────

def check_and_redeem(funder: str, private_key: str) -> float:
    """
    Non-blocking. Call before each bet cycle.

    - Collects any USDC redeemed by a previous background run (add to bankroll.balance)
    - If new winning positions exist and no redemption is in flight, starts one in background
    - Returns immediately — never blocks trading

    Returns: USDC redeemed since last call (0 if nothing yet).
    """
    global _redemption_thread

    # 1. Collect completed redemptions from background
    collected = pop_redeemed()
    if collected > 0:
        print(f"[redeemer] Collected ${collected:.2f} from background redemption")

    # 2. Start a new background redemption if none is running
    if _redemption_thread is None or not _redemption_thread.is_alive():
        try:
            winning = _get_winning_positions(funder)
            if winning:
                total = sum(float(p.get("currentValue", 0)) for p in winning)
                print(f"[redeemer] ${total:.2f} to redeem — starting background thread")
                _redemption_thread = threading.Thread(
                    target=_bg_redeem_worker,
                    args=(funder, private_key, winning),
                    daemon=True,   # won't block process exit
                )
                _redemption_thread.start()
        except Exception as e:
            print(f"[redeemer] check failed: {e}")

    return collected
