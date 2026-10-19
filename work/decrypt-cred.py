# Decrypt AutoClaw-official v10 credential file; print accessToken to stdout.
# Usage: python decrypt-cred.py   (caller captures stdout into a variable)
#
# Basis: docs/autoclaw-09-egressauditing-and-credential-decryption.md sec 3.2
#   v10 = "v10" || nonce(12) || ciphertext || tag(16)
#   master key = Local State -> os_crypt.encrypted_key (base64)
#                -> strip "DPAPI" 5-byte prefix
#                -> CryptUnprotectData(CurrentUser) -> 32B AES-256
#
# ASCII-only on purpose: the bat/powershell handoff on this box mangles UTF-8.
import io
import json
import base64
import os
import sys

import win32crypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = os.environ.get('AUTOCLAW_USER_DATA_ROOT', os.path.expandvars(r'%APPDATA%/AutoClaw-official'))
# 账号目录名 = sha256(accountId)。默认取 mtime 最新的一个，避免硬编码本机账号。
def _pick_account():
    env = os.environ.get('AUTOCLAW_ACCOUNT')
    if env:
        return env
    root = os.path.join(BASE, 'accounts')
    subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    if not subs:
        raise SystemExit('no account dir under %s' % root)
    return max(subs, key=lambda d: os.path.getmtime(os.path.join(root, d)))

ACCT = _pick_account()


def note(msg):
    sys.stderr.write(msg + '\n')


# 1) master key
ls = json.load(io.open(os.path.join(BASE, 'Local State'), encoding='utf-8'))
raw = base64.b64decode(ls['os_crypt']['encrypted_key'])
assert raw[:5] == b'DPAPI', 'Local State key is not DPAPI-prefixed'
key = win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1]
note('master key: %d bytes' % len(key))

# 2) credential file
cred_path = os.path.join(BASE, 'accounts', ACCT, 'account-credentials.enc')
b = io.open(cred_path, 'rb').read()
assert b[:3] == b'v10', 'credential file is not v10'
plain = AESGCM(key).decrypt(b[3:15], b[15:], None).decode('utf-8')
cred = json.loads(plain)

token = (cred.get('accessToken') or '').strip()
if not token:
    note('ERROR: no accessToken in credential file')
    sys.exit(3)

note('token: %s... (len=%d)' % (token[:14], len(token)))
note('cred updatedAt: %s' % cred.get('updatedAt'))

# JWT expiry, so an expired token is not discovered by burning a request
try:
    import datetime
    p = token.split('.')[1]
    p += '=' * (-len(p) % 4)
    claims = json.loads(base64.urlsafe_b64decode(p))
    exp = claims.get('exp')
    if exp:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        note('token exp: %s (%.1f h from now)' % (
            datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat(),
            (exp - now) / 3600.0))
except Exception as e:
    note('jwt parse skipped: %s' % e)

sys.stdout.write(token)
