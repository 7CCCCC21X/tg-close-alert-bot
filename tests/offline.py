"""Imported first by every suite: any real HTTP request fails at once, so results never depend on the network."""
import urllib.request


def _no_network(request, *args, **kwargs):
    raise OSError(f"network disabled in tests: {getattr(request, 'full_url', request)}")


urllib.request.urlopen = _no_network


import archived_markets  # noqa: E402  (imports main: the markets that have ended stay covered by the suites)

archived_markets.install()
