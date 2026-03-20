"""
Global SSL verification bypass for ProtonVPN compatibility.
ProtonVPN injects a self-signed cert, causing SSL errors.
Import this module early in any entry point to patch all requests/urllib calls.
"""
import ssl
import urllib.request
import warnings

import requests
import urllib3

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# Patch requests globally — all requests.get/post/Session calls
_original_request = requests.Session.request

def _patched_request(self, method, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _original_request(self, method, url, **kwargs)

requests.Session.request = _patched_request

# Patch urllib (used by telegram_control.py)
_unverified_ctx = ssl.create_default_context()
_unverified_ctx.check_hostname = False
_unverified_ctx.verify_mode = ssl.CERT_NONE
urllib.request.install_opener(
    urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_unverified_ctx)
    )
)
