"""HTTP defaults shared by device transports."""

import urllib.request


DEVICE_USER_AGENT = "ButtonBox/0.1.0"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a device credential to a redirected destination."""

    def redirect_request(self, *_args, **_kwargs):
        return None
