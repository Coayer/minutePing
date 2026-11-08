#!/usr/bin/env python3
"""Builds ca.pem: the root certificates the board needs to verify HTTPS.

Stock MicroPython ESP32 firmware ships no root store -- the ESP-IDF certificate
bundle is disabled in the port's sdkconfig.base, commented "not implemented in
MicroPython" -- so without a CA file the board can encrypt a connection but
cannot tell who is on the other end. This writes one containing only the roots
you actually need.

    python3 scripts/fetch_ca.py discord.com
    python3 scripts/fetch_ca.py --common discord.com smtp.gmail.com

For each host it opens a real TLS connection, verifies it against this
machine's trust store, and keeps the self-signed root the chain terminates at.
Roots are long-lived (Discord's is good until 2036) and the 90-day leaf
rotations underneath are invisible to the board.

--common adds the roots behind the major public CAs as well, discovered the
same way from reference hosts. That costs a few KB and means a host switching
CA vendors -- the one realistic way pinning breaks -- does not silently stop
your alerts.

The result is verified before it is written: every host is re-checked against
the new bundle alone, so a file that would not work on the board is never
produced. Upload it next to config.json and set "tls": {"ca_file": "ca.pem"}.
"""
import argparse
import os
import socket
import ssl
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One well-known host per major public CA. Their roots are what a host is
# likely to move to if it ever changes vendor.
REFERENCE_HOSTS = (
    # Let's Encrypt issues from two roots; X1 (RSA) is the most widely
    # deployed root on the web, and letsencrypt.org itself serves only X2.
    "valid-isrgrootx1.letsencrypt.org",  # ISRG Root X1
    "letsencrypt.org",     # ISRG Root X2
    "www.google.com",      # GTS Root
    "www.amazon.com",      # Amazon Root CA
    "www.digicert.com",    # DigiCert Global Root
    "www.globalsign.com",  # GlobalSign Root
)

# mbedTLS parses the whole file into heap at context creation, so this stays a
# curated handful rather than the ~140-cert bundle a desktop carries.
SIZE_WARNING_BYTES = 24000


def root_for(host, port=443, timeout=10):
    """Returns the DER of the self-signed root that `host` chains up to."""
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secure:
            chain = secure.get_verified_chain()

    if not chain:
        raise RuntimeError("no certificate chain returned")

    return chain[-1]


def describe(pem):
    """Subject and expiry, if openssl is around to read them. Cosmetic only."""
    try:
        result = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-enddate"],
            input=pem, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "(unidentified certificate)"

    if result.returncode != 0:
        return "(unidentified certificate)"

    fields = dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines()
        if "=" in line)
    subject = fields.get("subject", "?").strip()
    expiry = fields.get("notAfter", "?").strip()

    if "CN = " in subject:
        subject = subject.split("CN = ", 1)[1]

    return "{} (expires {})".format(subject, expiry)


def verify_against(path, hosts):
    """Re-checks every host trusting only the new bundle. Returns failures."""
    failures = []

    for host in hosts:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=path)

        try:
            with socket.create_connection((host, 443), timeout=10) as raw:
                with context.wrap_socket(raw, server_hostname=host):
                    pass
        except (OSError, ssl.SSLError) as error:
            failures.append((host, error))

    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="+",
                        help="hostnames the board will connect to over https")
    parser.add_argument("--common", action="store_true",
                        help="also include the roots of the major public CAs")
    parser.add_argument("-o", "--output", default=os.path.join(ROOT, "ca.pem"))
    args = parser.parse_args()

    wanted = list(args.hosts)
    if args.common:
        wanted += [host for host in REFERENCE_HOSTS if host not in wanted]

    roots = {}  # DER -> PEM, so the same root shared by two hosts lands once
    failed = []

    for host in wanted:
        try:
            der = root_for(host)
        except (OSError, ssl.SSLError, RuntimeError) as error:
            failed.append((host, error))
            print("  {}: FAILED ({})".format(host, error), file=sys.stderr)
            continue

        pem = ssl.DER_cert_to_PEM_cert(der)
        known = der in roots
        roots[der] = pem
        print("  {}: {}{}".format(
            host, describe(pem), " [already included]" if known else ""))

    if failed and not roots:
        print("\nNo roots could be collected.", file=sys.stderr)
        return 1

    # A host named on the command line must genuinely work; a reference host
    # that is unreachable only costs some resilience.
    required = [host for host, _ in failed if host in args.hosts]
    if required:
        print("\nCould not reach: {}. Fix that first -- a bundle missing "
              "these roots would stop the board's alerts.".format(
                  ", ".join(required)), file=sys.stderr)
        return 1

    body = "".join(roots[der] for der in roots)
    header = ("# Generated by scripts/fetch_ca.py for: {}\n"
              "# {} root certificate(s). Upload alongside config.json.\n".format(
                  " ".join(args.hosts), len(roots)))
    contents = header + body

    with open(args.output, "w") as handle:
        handle.write(contents)

    print("\nVerifying {} against the new bundle...".format(
        ", ".join(args.hosts)))
    problems = verify_against(args.output, args.hosts)
    if problems:
        os.remove(args.output)
        for host, error in problems:
            print("  {}: {}".format(host, error), file=sys.stderr)
        print("\nBundle rejected and removed; the board would not have been "
              "able to verify these hosts.", file=sys.stderr)
        return 1

    print("  all hosts verified.")
    print("\nWrote {} ({} bytes, {} roots).".format(
        args.output, len(contents), len(roots)))

    if len(contents) > SIZE_WARNING_BYTES:
        print("Note: that is fairly large. mbedTLS parses the whole file into "
              "heap, so trim it if the board reports memory errors.")

    print('\nNext: upload it and set "tls": {"ca_file": "ca.pem"} in config.json.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
