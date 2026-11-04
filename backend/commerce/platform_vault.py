"""Server-only encrypted credential storage. The local key is never returned by an API."""
import json
import os
from cryptography.fernet import Fernet, InvalidToken
from .config import ROOT, settings
from .platform_client import PlatformError


def cipher():
    value = settings.platform_encryption_key
    if not value:
        path = ROOT / '.runtime/platform-encryption.key'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
        value = path.read_bytes().strip()
    try:
        return Fernet(value)
    except (ValueError, TypeError):
        raise PlatformError('configuration', '平台凭证加密配置无效，请检查本机密钥。')


def encrypt(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def decrypt(value):
    if not value:
        return {}
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise PlatformError('configuration', '无法解密已有平台凭证，请恢复原加密密钥或重新填写凭证。')
