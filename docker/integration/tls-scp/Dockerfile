# syntax=docker/dockerfile:1
#
# A minimal TLS-only DICOM SCP for the integration tier's TLS test.
#
# A pynetdicom AE rather than dcmtk's storescp: the client under test
# (services/upload/dicom_store.py) builds a plain ssl.SSLContext and hands it
# to pynetdicom's own association API, so an SCP built the same way proves
# xnatctl's actual TLS wiring against a real peer instead of also exercising
# a second, unrelated TLS stack (dcmtk's) that nothing in this codebase uses.
#
# The cert is generated fresh by entrypoint.sh on every container start, not
# baked in at build time: baking it in would make the cert as stale as the
# last image build and shared across every checkout using that image, for a
# throwaway credential that costs about a second to regenerate.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pydicom>=2.4.0" "pynetdicom>=2.0.0"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scp.py /usr/local/bin/scp.py
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 11112
ENTRYPOINT ["entrypoint.sh"]
