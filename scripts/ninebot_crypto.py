from __future__ import annotations

import base64
import hashlib
import json
import secrets
import textwrap
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDT3m0c/8y9c13PzaFbATEg+Zwd
kpPcCy0V21VKBBSx16ckVtLERAQ7EH8d6DqgEbyzayAwlQd1gDhUmx27hDWafXr9
/evUZkkBegcsNnKrIlh93lPKccjk+LDXS1TnDIIFiTlSbNnaYwehI/9pUbKCI3h7
yE0pum6hJh/9QtGPlwIDAQAB
-----END PUBLIC KEY-----"""

REQUEST_KEY_CHARSET = ''.join(chr(i) for i in range(0x21, 0x7f))
KEY_DATA_FOUR_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789;-_.,+/"
HEX_LOWER = "0123456789abcdef"


@dataclass(frozen=True)
class KeyData:
    one: str
    two: str
    three: str
    four: str

    def as_wrapper_fields(self) -> dict[str, str]:
        return {
            "keyDataFour": self.four,
            "keyDataOne": self.one,
            "keyDataThree": self.three,
            "keyDataTwo": self.two,
        }


def _rand_from_charset(length: int, charset: str) -> str:
    return ''.join(charset[secrets.randbelow(len(charset))] for _ in range(length))


def gen_request_key() -> bytes:
    return _rand_from_charset(16, REQUEST_KEY_CHARSET).encode("ascii")


def gen_key_data() -> KeyData:
    return KeyData(
        one=_rand_from_charset(11, HEX_LOWER),
        two=_rand_from_charset(11, HEX_LOWER),
        three=_rand_from_charset(10, HEX_LOWER),
        four=_rand_from_charset(8, KEY_DATA_FOUR_CHARSET),
    )


def gen_nonce() -> str:
    raw = secrets.token_bytes(16)
    # ninecli emits the first byte in lower-case hex and the remaining bytes in upper-case hex.
    return raw[:1].hex() + raw[1:].hex().upper()


def compute_checkcode(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def compact_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_payload_json(fields: Mapping[str, Any], *, service_time_ms: int, nonce: str | None = None) -> str:
    body = OrderedDict(fields)
    body["serviceTime"] = service_time_ms
    body["nonce"] = nonce or gen_nonce()
    unsigned = compact_json(body)
    body["checkcode"] = compute_checkcode(unsigned)
    return compact_json(body)


def _json_quote_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _base64_with_go_line_breaks(data: bytes) -> str:
    # ninecli wraps base64 at 76 chars and keeps a trailing newline before JSON-escaping it.
    encoded = base64.b64encode(data).decode("ascii")
    return "\n".join(textwrap.wrap(encoded, 76)) + "\n"


def build_wrapper(payload_json: str, key_data: KeyData, *, platform: int, timestamp_s: int) -> str:
    data = _base64_with_go_line_breaks(payload_json.encode("utf-8"))
    return (
        "{\n"
        f"\t\"data\" : {_json_quote_string(data)},\n"
        f"\t\"keyDataFour\" : \"{key_data.four}\",\n"
        f"\t\"keyDataOne\" : \"{key_data.one}\",\n"
        f"\t\"keyDataThree\" : \"{key_data.three}\",\n"
        f"\t\"keyDataTwo\" : \"{key_data.two}\",\n"
        f"\t\"platform\" : {platform},\n"
        f"\t\"timeStamp\" : {timestamp_s}\n"
        "}\n"
    )


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("invalid PKCS7 data length")
    pad = data[-1]
    if pad < 1 or pad > block_size or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad]


def aes_cbc_encrypt_zero_iv(plaintext: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv=bytes(16)).encrypt(pkcs7_pad(plaintext, 16))


def aes_cbc_decrypt_zero_iv(ciphertext: bytes, key: bytes) -> bytes:
    return pkcs7_unpad(AES.new(key, AES.MODE_CBC, iv=bytes(16)).decrypt(ciphertext), 16)


def rsa_encrypt_request_key(request_key: bytes) -> bytes:
    return PKCS1_v1_5.new(RSA.import_key(RSA_PUBLIC_KEY_PEM)).encrypt(request_key)


def encrypt_request(payload_json: str, *, platform: int = 2, timestamp_s: int | None = None) -> tuple[dict[str, str], KeyData, bytes, str]:
    import time

    key_data = gen_key_data()
    request_key = gen_request_key()
    wrapper = build_wrapper(payload_json, key_data, platform=platform, timestamp_s=timestamp_s or int(time.time()))
    wrapper_bytes = wrapper.encode("utf-8")
    ciphertext = aes_cbc_encrypt_zero_iv(wrapper_bytes, request_key)
    envelope = {
        "d": base64.b64encode(ciphertext).decode("ascii"),
        "h": hashlib.md5(wrapper_bytes).hexdigest(),
        "k": base64.b64encode(rsa_encrypt_request_key(request_key)).decode("ascii"),
        "p": "101",
        "t": "0",
    }
    return envelope, key_data, request_key, wrapper


def _ror32(value: int, bits: int) -> int:
    value &= 0xFFFFFFFF
    return ((value >> bits) | ((value << (32 - bits)) & 0xFFFFFFFF)) & 0xFFFFFFFF


def _derive_acc(value: str) -> int:
    acc = 0
    for b in value.encode("utf-8"):
        acc = (_ror32(acc, 24) ^ b) & 0xFFFFFFFF
    return acc


def derive_response_key(key_data: KeyData) -> bytes:
    x5 = _derive_acc(key_data.one)
    x1 = _derive_acc(key_data.two)
    x2 = _derive_acc(key_data.three)
    x3 = _derive_acc(key_data.four)

    x4 = (x1 ^ x3) & 0xFFFFFFFF
    x6 = (x5 ^ x2) & 0xFFFFFFFF
    x7 = (x6 ^ _ror32(x6, 24)) & 0xFFFFFFFF
    x6 = (x7 ^ _ror32(x6, 8)) & 0xFFFFFFFF
    x7 = (x1 ^ x6) & 0xFFFFFFFF
    x3 = (x3 ^ x6) & 0xFFFFFFFF
    x6 = (x4 ^ _ror32(x4, 24)) & 0xFFFFFFFF
    x4 = (x6 ^ _ror32(x4, 8)) & 0xFFFFFFFF
    x6 = (x2 ^ x4) & 0xFFFFFFFF
    x4 = (x5 ^ x4) & 0xFFFFFFFF
    x5 = (x6 & x7) & 0xFFFFFFFF
    x4 = (x5 ^ x4) & 0xFFFFFFFF
    word_28 = x4
    x5 = (x6 | x3) & 0xFFFFFFFF
    x6 = (x3 ^ x6) & 0xFFFFFFFF
    x8 = (x5 ^ x7) & 0xFFFFFFFF
    x6 = (x8 ^ (~x6 & 0xFFFFFFFF)) & 0xFFFFFFFF
    x9 = (x4 ^ x6) & 0xFFFFFFFF
    word_34 = x9
    x4 = ((x4 | x6) ^ x8) & 0xFFFFFFFF
    word_30 = x4
    x4 = (x5 ^ (~x7 & 0xFFFFFFFF)) & 0xFFFFFFFF
    x4 = (x9 & x4) & 0xFFFFFFFF
    x3 = (x4 ^ x3) & 0xFFFFFFFF
    word_2c = x3
    return b"".join(w.to_bytes(4, "little") for w in (word_2c, word_30, word_34, word_28))


def decrypt_response_body(response_json: Mapping[str, Any], key_data: KeyData) -> bytes:
    encrypted = response_json.get("r")
    if not isinstance(encrypted, str):
        raise ValueError("response does not contain encrypted string field 'r'")
    ciphertext = base64.b64decode(encrypted)
    wrapper_bytes = aes_cbc_decrypt_zero_iv(ciphertext, derive_response_key(key_data))
    wrapper = json.loads(wrapper_bytes.decode("utf-8"))
    data = wrapper.get("data")
    if not isinstance(data, str):
        raise ValueError("decrypted wrapper does not contain string field 'data'")
    compact_b64 = ''.join(ch for ch in data if ch not in ' \t\r\n')
    return base64.b64decode(compact_b64)
