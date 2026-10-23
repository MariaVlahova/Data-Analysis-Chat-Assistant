"""Shorten Google's credential probe before any Google library is imported (plan 03).

When there is no key file and no `gcloud` login, google-auth asks the Compute Engine metadata server whether it
runs on Google Cloud: 3 seconds per try, 3 tries. On a laptop that is a silent stall of about 9 seconds at the start
of `chat` and `doctor`, before the "no credentials" message. A real metadata server answers in milliseconds, so
1 second and 1 try still find credentials on Compute Engine, Cloud Run and workload identity. The probe is not
switched off (`NO_GCE_CHECK`): that would hide those credentials.

google-auth reads both limits once, when it is first imported, so this module must be imported first by anything that
imports it. `bq_client.py` and the `doctor` credentials check do. A value you set yourself is kept.
"""
import os

os.environ.setdefault("GCE_METADATA_TIMEOUT", "1")
os.environ.setdefault("GCE_METADATA_DETECT_RETRIES", "1")
