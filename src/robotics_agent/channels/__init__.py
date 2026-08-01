"""Kanal adaptorleri: REST API ve kimlik dogrulama.

Kimlik, tenant, organizasyon kapsami, rate limit ve prompt guvenlik kontrolleri
kanal katmaninda uygulanir. Agent cekirdegi kanaldan bagimsizdir; yeni bir kanal
(Fiori/Work Zone, Joule Studio, Teams) ayni `ActorContext` sozlesmesini uretmekle
yukumludur.
"""

from .auth import AuthenticationError, Authenticator, Principal, RateLimiter, hash_token

__all__ = [
    "AuthenticationError",
    "Authenticator",
    "Principal",
    "RateLimiter",
    "hash_token",
]
