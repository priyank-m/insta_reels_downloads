"""TestClient wrapper that calls the new APIs (/history, /payment except the webhook) like the
apps do: JSON bodies encrypted as text/plain, answers decrypted. Other paths pass through."""
import json as jsonlib

from app.core import client_crypto
from app.middleware.legacy_encryption import is_new_api


class DecryptedResponse:
    def __init__(self, response):
        self.raw = response
        self.status_code = response.status_code
        self.headers = response.headers
        try:
            self.text = client_crypto.decrypt(response.text)
            self.encrypted = True
        except ValueError:
            self.text = response.text  # e.g. the plain 500 when the key is missing
            self.encrypted = False

    def json(self):
        return jsonlib.loads(self.text)


class EncryptedClient:
    def __init__(self, client):
        self.raw = client

    def request(self, method, url, json=None, **kwargs):
        if not is_new_api(url.split("?")[0]):
            if json is not None:
                kwargs["json"] = json
            return self.raw.request(method, url, **kwargs)
        headers = dict(kwargs.pop("headers", None) or {})
        if json is not None:
            kwargs["content"] = client_crypto.encrypt(jsonlib.dumps(json))
            headers["Content-Type"] = "text/plain"
        return DecryptedResponse(self.raw.request(method, url, headers=headers, **kwargs))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)
