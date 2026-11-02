"""One authenticated-encryption key loader for private stored material."""
import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

def storage_cipher():
    try:
        value=base64.b64decode(os.environ.get('AFTERSPAN_CONNECTION_KEY',''),altchars=b'-_',validate=True)
        if len(value)!=32:raise ValueError()
        return AESGCM(value)
    except (ValueError,TypeError):raise HTTPException(503,'Private storage is not configured') from None
