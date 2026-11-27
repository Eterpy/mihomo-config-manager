import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class TestKeys:
    __test__ = False

    def __init__(self):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"

    def jwks(self):
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        key.update(kid=self.kid, alg="RS256", use="sig")
        return {"keys": [key]}

    def token(self, settings, **overrides):
        claims = {"iss": settings.issuer, "aud": [settings.audience], "sub": "test-admin", "iat": int(time.time()), "exp": int(time.time()) + 3600, "type": "app"}
        claims.update(overrides)
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


def link(client, item):
    response = client.post(f'/api/configs/{item["id"]}/subscription-link')
    response.raise_for_status()
    return response.json()["subscription_url"]


def revision(client, base):
    row = client.get(base).json()
    return {"expected_updated_at": row["updated_at"]}
