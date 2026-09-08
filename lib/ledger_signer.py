"""
Ledger hardware wallet signer for Offerbook transactions.

Talks to the Ledger Solana app directly over USB HID using the official
APDU protocol (github.com/LedgerHQ/app-solana/blob/develop/doc/api.md),
matching the chunking behaviour of @ledgerhq/hw-app-solana.

Requires:  pip install ledgerblue hidapi base58 solders

Usage:
    from ledger_signer import LedgerSigner

    signer = LedgerSigner(path="44'/501'/0'")
    wallet_pubkey = signer.get_pubkey()          # confirm this matches your address
    signed_b64 = signer.sign_transaction(tx_b64)  # prompts for approval on-device
"""
from __future__ import annotations

import base64
import hashlib

import base58
from ledgerblue.comm import CommException, getDongle
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

CLA = 0xE0

INS_GET_APP_CONFIGURATION = 0x04
INS_GET_PUBKEY = 0x05
INS_SIGN = 0x06

P1_NON_CONFIRM = 0x00
P1_CONFIRM = 0x01

P2_INIT = 0x00
P2_EXTEND = 0x01
P2_MORE = 0x02

MAX_PAYLOAD = 255

SW_USER_REJECTED = 0x6982
SW_BLIND_SIGN_REQUIRED = 0x6808

DEFAULT_PATH = "44'/501'/0'"


class LedgerError(RuntimeError):
    pass


def _encode_path(path_str: str) -> bytes:
    parts = path_str.split("/")
    out = bytes([len(parts)])
    for p in parts:
        num = int(p.rstrip("'")) | 0x80000000  # Solana paths are always hardened
        out += num.to_bytes(4, "big")
    return out


def _describe_transaction(tx: VersionedTransaction, message_bytes: bytes) -> str:
    """
    Human-readable dump of a transaction's accounts and instructions, printed
    right before it's sent to the Ledger for approval. The Ledger's own screen
    can't show Offerbook's custom instructions (blind signing), so this is the
    only real chance to visually catch a wrong account, amount, or program
    before an approval — which, unlike a hot-wallet tx, can't be walked back
    once it lands on-chain.

    Also includes the SHA-256 "Message Hash" of message_bytes, base58-encoded
    — the Solana app's own firmware computes and displays this exact value
    on-device during blind signing (see handle_sign_message.c in
    LedgerHQ/app-solana, cx_hash_sha256(G_command.message, ...) -> "Message
    Hash" summary item; base58 because that's how the Solana ecosystem
    displays every 32-byte value — pubkeys, signatures, hashes — confirmed
    against an actual device screen, which shows a 44-char base58 string,
    not hex). Comparing it against the device screen catches a
    tampered/substituted transaction that this console output alone couldn't.
    """
    msg = tx.message
    account_keys = [str(k) for k in msg.account_keys]
    message_hash = base58.b58encode(hashlib.sha256(message_bytes).digest()).decode()

    lines = []
    lines.append("=" * 78)
    lines.append("TRANSACTION TO SIGN — review before approving on the Ledger")
    lines.append("=" * 78)
    lines.append(f"Message Hash     : {message_hash}")
    lines.append("  ^ compare this against the \"Message Hash\" shown on your Ledger screen")
    lines.append(f"Recent blockhash : {msg.recent_blockhash}")
    lines.append(f"Fee payer        : {account_keys[0]}")
    lines.append("")
    lines.append(f"Accounts ({len(account_keys)}):")
    for i, key in enumerate(account_keys):
        tag = " (signer)" if msg.is_signer(i) else ""
        lines.append(f"  [{i}] {key}{tag}")
    lines.append("")
    lines.append(f"Instructions ({len(msg.instructions)}):")
    for i, ix in enumerate(msg.instructions):
        program = account_keys[ix.program_id_index]
        lines.append(f"  [{i}] program: {program}")
        for acc_idx in bytes(ix.accounts):
            lines.append(f"        account[{acc_idx}]: {account_keys[acc_idx]}")
        lines.append(f"        data (hex): {bytes(ix.data).hex()}")
    lines.append("=" * 78)
    return "\n".join(lines)


class LedgerSigner:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._dongle = None

    def _get_dongle(self):
        if self._dongle is None:
            try:
                self._dongle = getDongle(debug=False)
            except Exception as exc:
                raise LedgerError(
                    f"Could not connect to Ledger device: {exc}\n"
                    "Make sure it's plugged in, unlocked, and the Solana app is open."
                ) from exc
        return self._dongle

    def get_app_configuration(self) -> dict:
        dongle = self._get_dongle()
        apdu = bytes([CLA, INS_GET_APP_CONFIGURATION, 0x00, 0x00, 0x00])
        try:
            r = dongle.exchange(apdu)
        except CommException as exc:
            raise LedgerError(self._friendly_error(exc)) from exc
        return {
            "blind_sign_enabled": bool(r[0]),
            "pubkey_display": r[1],
            "version": f"{r[2]}.{r[3]}.{r[4]}",
        }

    def get_pubkey(self, display: bool = False) -> str:
        dongle = self._get_dongle()
        data = _encode_path(self.path)
        apdu = bytes([CLA, INS_GET_PUBKEY, 0x01 if display else 0x00, 0x00, len(data)]) + data
        try:
            result = dongle.exchange(apdu)
        except CommException as exc:
            raise LedgerError(self._friendly_error(exc)) from exc
        return base58.b58encode(result[:32]).decode()

    def _sign_bytes(self, message_bytes: bytes) -> bytes:
        dongle = self._get_dongle()
        payload = bytes([1]) + _encode_path(self.path) + message_bytes  # 1 signer

        offset = 0
        p2 = P2_INIT
        result = b""
        while len(payload) - offset > MAX_PAYLOAD:
            chunk = payload[offset : offset + MAX_PAYLOAD]
            offset += MAX_PAYLOAD
            apdu = bytes([CLA, INS_SIGN, P1_CONFIRM, p2 | P2_MORE, len(chunk)]) + chunk
            try:
                dongle.exchange(apdu)
            except CommException as exc:
                raise LedgerError(self._friendly_error(exc)) from exc
            p2 |= P2_EXTEND

        chunk = payload[offset:]
        apdu = bytes([CLA, INS_SIGN, P1_CONFIRM, p2, len(chunk)]) + chunk
        try:
            result = dongle.exchange(apdu)
        except CommException as exc:
            raise LedgerError(self._friendly_error(exc)) from exc
        return bytes(result)

    def sign_transaction(self, tx_b64: str, expected_signer: str | None = None) -> str:
        """
        Sign a base64-encoded Solana transaction and return the signed,
        base64-encoded transaction (ready to broadcast).
        """
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)

        signer_pubkey = Pubkey.from_string(expected_signer) if expected_signer else None
        account_keys = list(tx.message.account_keys)
        if signer_pubkey is not None:
            if signer_pubkey not in account_keys:
                raise LedgerError(f"Signer {expected_signer} is not a required signer of this transaction")
            signer_index = account_keys.index(signer_pubkey)
        else:
            signer_index = 0

        # Hardware wallets sign only the message bytes, not the full wire-format
        # transaction (which includes empty signature placeholder slots).
        message_bytes = bytes(tx.message)

        print(_describe_transaction(tx, message_bytes))

        sig_bytes = self._sign_bytes(message_bytes)

        signatures = list(tx.signatures)
        signatures[signer_index] = Signature(sig_bytes)
        signed_tx = VersionedTransaction.populate(tx.message, signatures)

        if not all(signed_tx.verify_with_results()):
            raise LedgerError("Ledger returned a signature that failed verification — not broadcasting")

        return base64.b64encode(bytes(signed_tx)).decode()

    @staticmethod
    def _friendly_error(exc: CommException) -> str:
        if exc.sw == SW_USER_REJECTED:
            return "Rejected on the Ledger device."
        if exc.sw == SW_BLIND_SIGN_REQUIRED:
            return (
                "Blind signing is required but disabled on the device. "
                "Enable it in the Solana app's settings and try again."
            )
        return f"Ledger error: {exc}"
