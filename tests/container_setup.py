"""Only used by the disposable Compose test stack; never production."""
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from tests.helpers import TestKeys

for name, mode in [("/data", 0o700), ("/data/private", 0o700), ("/data/db", 0o700), ("/data/published", 0o755), ("/fixtures", 0o755)]:
    path = Path(name)
    path.mkdir(exist_ok=True, parents=True)
    os.chown(path, 10001, 10001)
    os.chmod(path, mode)
path = Path("/fixtures/test-only-key.pem")
# `compose run runner` re-runs this dependency while admin is already up.
# Regenerating the key would make admin reject every runner JWT.
if not path.exists():
    key = TestKeys().private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.write_bytes(key)
os.chown(path, 10001, 10001)
os.chmod(path, 0o400)
