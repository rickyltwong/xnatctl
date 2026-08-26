#!/usr/bin/env python3
"""Minimal TLS-only DICOM SCP for the integration tier.

Accepts a C-ECHO over TLS and nothing else -- that is all
``tests/integration/test_dicom_tls.py`` needs to prove that the association
code in ``services/upload/dicom_store.py`` actually negotiates TLS against a
real peer, not just a mocked ``ssl.SSLContext``.
"""

from __future__ import annotations

import argparse
import ssl

from pynetdicom import AE, evt
from pynetdicom.events import Event


def _verification_sop_class() -> str:
    """Match dicom_store.py's own cross-version lookup for this UID."""
    from pynetdicom import sop_class

    verification_uid = "1.2.840.10008.1.1"
    return str(
        getattr(sop_class, "VerificationSOPClass", getattr(sop_class, "Verification", verification_uid))
    )


def handle_echo(_event: Event) -> int:
    """Accept every C-ECHO unconditionally: 0x0000 is DICOM's Success status."""
    return 0x0000


def main() -> None:
    """Parse the cert/key/port/AET args and block, serving C-ECHO over TLS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True, help="Server certificate (PEM)")
    parser.add_argument("--key", required=True, help="Server private key (PEM)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--aet", required=True)
    args = parser.parse_args()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)

    ae = AE(ae_title=args.aet)
    ae.add_supported_context(_verification_sop_class())

    print(
        f"tls-scp: listening on 0.0.0.0:{args.port} as AE '{args.aet}' (TLS required)",
        flush=True,
    )
    ae.start_server(
        ("0.0.0.0", args.port),
        ssl_context=context,
        evt_handlers=[(evt.EVT_C_ECHO, handle_echo)],
    )


if __name__ == "__main__":
    main()
