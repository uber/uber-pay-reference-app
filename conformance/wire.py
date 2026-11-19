"""Raw HTTP and signing primitives shared by tests and the interactive simulator."""

import base64
import hashlib
import http.client
import json
import re
import time
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def keys(directory):
    directory.mkdir(parents=True, exist_ok=True)
    private_path, public_path = (
        directory / "uber-private.pem",
        directory / "uber-public.pem",
    )
    if private_path.exists() and public_path.exists():
        return serialization.load_pem_private_key(private_path.read_bytes(), None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return key


@dataclass
class Reply:
    status: int
    headers: dict
    raw: bytes

    @property
    def body(self):
        return json.loads(self.raw) if self.raw else None


class Client:
    def __init__(self, url, key, key_id="demo-uber-key", control_secret=""):
        self.base = urlsplit(url)
        self.prefix = self.base.path.rstrip("/")
        self.key, self.key_id, self.control_secret = key, key_id, control_secret
        self.observed = set()

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        idem=None,
        token=None,
        signed=True,
        raw=None,
        headers=None,
        date=None,
        signature_target=None,
        signature_parameters=None,
        key_id_override=None,
        outside=False,
    ):
        target = path if outside else self.prefix + path
        body = (
            raw if raw is not None else encode(payload) if payload is not None else b""
        )
        host = self.base.netloc
        date = date or formatdate(usegmt=True)
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        values = {
            "Host": host,
            "Content-Type": "application/json",
            "Accept-Language": "en-US",
            "Date": date,
            "Digest": digest,
        }
        if signed:
            text = (
                f"(request-target): {method.lower()} {signature_target or target}\n"
                f"host: {host}\ndate: {date}\ndigest: {digest}"
            )
            signature = self.key.sign(
                text.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            values["Signature"] = (
                f'keyId="{key_id_override or self.key_id}",algorithm="rsa-sha256",'
                'headers="(request-target) host date digest",'
                f'signature="{base64.b64encode(signature).decode()}"'
            )
            for name, value in (signature_parameters or {}).items():
                values["Signature"] += f', {name}="{value}"'
        if idem is not None:
            values["X-Idempotency-Key"] = idem
        if token:
            values["Authorization"] = "Bearer " + token
        values.update(headers or {})
        connection_type = (
            http.client.HTTPSConnection
            if self.base.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(self.base.hostname, self.base.port, timeout=8)
        try:
            connection.request(method, target, body=body, headers=values)
            r = connection.getresponse()
            reply = Reply(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
            template = re.sub(
                r"^/charge/[^/]+/(capture|cancel)$",
                r"/charge/{id}/\1",
                path.split("?", 1)[0],
            )
            if not outside:
                self.observed.add((method, template, r.status))
            return reply
        finally:
            connection.close()

    def control(self, method, path, payload=None):
        return self.request(
            method,
            "/_test/" + path,
            payload,
            signed=False,
            outside=True,
            headers={"X-Test-Control-Secret": self.control_secret},
        )


def eventually(fn, *, timeout=8, description="condition"):
    until = time.monotonic() + timeout
    error = None
    while time.monotonic() < until:
        try:
            value = fn()
            if value:
                return value
        except (OSError, AssertionError) as exc:
            error = exc
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}; last error={error}")
