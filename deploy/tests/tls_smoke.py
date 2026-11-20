"""Проверка TLS, маршрутизации и SSE на изолированных контейнерах."""

import datetime as dt
import http.client
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

BACKEND = r"""
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health?probe=1":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.headers["X-Forwarded-Proto"].encode())
        elif self.path == "/api/chat/probe":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(b"data: second\n\n")
        else:
            self.send_error(404)

HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""


def docker(*args: str, check: bool = True) -> None:
    subprocess.run(["docker", *args], check=check, timeout=600)


def certificate(directory: Path) -> ssl.SSLContext:
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (directory / "fullchain.pem").write_bytes(pem)
    (directory / "privkey.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ssl.create_default_context(cadata=pem.decode("ascii"))


def verify(http_port: int, tls_port: int, context: ssl.SSLContext) -> None:
    origin = f"https://localhost:{tls_port}"
    path = "/sources/11111111-1111-4111-8111-111111111111"
    for attempt in range(50):
        conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=2)
        try:
            conn.request("GET", f"{path}?probe=1", headers={"Host": "unrelated.test"})
            reply = conn.getresponse()
            assert reply.status == 308 and reply.getheader("Location") == f"{origin}{path}?probe=1"
            break
        except OSError:
            # Docker может открыть порт раньше, чем Nginx начнёт отвечать.
            if attempt == 49:
                raise
            time.sleep(0.2)
        finally:
            conn.close()

    tls = http.client.HTTPSConnection("localhost", tls_port, context=context, timeout=5)
    try:
        tls.request("GET", path)
        reply = tls.getresponse()
        assert reply.status == 200 and b'<div id="root">' in reply.read()
        assert reply.getheader("Strict-Transport-Security")
        assert "frame-ancestors 'none'" in reply.getheader("Content-Security-Policy", "")
        tls.request("GET", "/api/health?probe=1", headers={"X-Forwarded-Proto": "http"})
        reply = tls.getresponse()
        assert reply.status == 200 and reply.read() == b"https"
        tls.request("POST", "/api/search", body=b"x" * (17 * 1024))
        reply = tls.getresponse()
        assert reply.status == 413
        reply.read()
        tls.request("GET", "/api/chat/probe")
        reply = tls.getresponse()
        assert reply.status == 200 and reply.getheader("Content-Type") == "text/event-stream"
        assert reply.readline() == b"data: first\n" and reply.readline() == b"\n"
        first_event = time.monotonic()
        assert reply.readline() == b"data: second\n"
        assert time.monotonic() - first_event >= 0.25, "Nginx буферизует SSE"
    finally:
        tls.close()


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    tag = secrets.token_hex(6)
    network, backend, nginx = (f"estafeta-tls-{part}-{tag}" for part in ("net", "api", "web"))
    image = "estafeta-tls-smoke:local"
    docker("build", "--target", "tls", "-t", image, "-f", str(root / "Dockerfile.frontend"), str(root))
    docker("network", "create", network)
    try:
        with tempfile.TemporaryDirectory() as folder:
            certificate_dir = Path(folder)
            context = certificate(certificate_dir)
            docker(
                "run", "-d", "--name", backend, "--network", network,
                "--network-alias", "backend", "python:3.12-slim-bookworm", "python", "-u", "-c",
                BACKEND,
            )
            with socket.socket() as http_socket, socket.socket() as tls_socket:
                http_socket.bind(("127.0.0.1", 0))
                tls_socket.bind(("127.0.0.1", 0))
                http_port = http_socket.getsockname()[1]
                tls_port = tls_socket.getsockname()[1]
            docker(
                "run", "-d", "--name", nginx, "--network", network,
                "-p", f"127.0.0.1:{http_port}:80", "-p", f"127.0.0.1:{tls_port}:443",
                "-e", f"AUTH_PUBLIC_ORIGIN=https://localhost:{tls_port}",
                "-e", "NGINX_ENVSUBST_FILTER=^(AUTH_PUBLIC_ORIGIN|ESTAFETA_TLS_HOST)$",
                "--mount", f"type=bind,source={certificate_dir},target=/run/estafeta/tls,readonly",
                image,
            )
            verify(http_port, tls_port, context)
    except (AssertionError, OSError, http.client.HTTPException, subprocess.CalledProcessError):
        docker("logs", nginx, check=False)
        raise
    finally:
        docker("rm", "-f", nginx, backend, check=False)
        docker("network", "rm", network, check=False)
    print("TLS, редирект, маршрутизация, лимит запроса и SSE проверены.")


if __name__ == "__main__":
    main()
