"""Sign in with Google: the browser gets an ID token from Google, and the server asks Google to confirm it.

Uses Google's tokeninfo endpoint, which checks the token's signature and expiry; we then check it was issued for
this app (GOOGLE_CLIENT_ID) and that the email is verified.
"""

import httpx

from .config import Settings

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class GoogleAuthError(RuntimeError):
    pass


class GoogleAuth:
    def __init__(self, settings: Settings):
        if not settings.google_client_id:
            raise RuntimeError("GOOGLE_CLIENT_ID is required for Google sign-in")
        self.client_id = settings.google_client_id
        self._http = httpx.Client(timeout=15)

    def verify(self, credential: str) -> dict:
        """Return {"sub", "email", "name"} for a valid Google ID token, else raise GoogleAuthError."""
        try:
            resp = self._http.get(TOKENINFO_URL, params={"id_token": credential})
        except httpx.HTTPError as exc:
            raise GoogleAuthError(f"Could not reach Google: {exc}") from exc
        if resp.status_code != 200:
            raise GoogleAuthError("Google sign-in failed. Please try again.")
        info = resp.json()
        if info.get("aud") != self.client_id or info.get("iss") not in GOOGLE_ISSUERS:
            raise GoogleAuthError("This sign-in was not issued for Intuvora.")
        if str(info.get("email_verified")).lower() != "true" or not info.get("email"):
            raise GoogleAuthError("Your Google email address is not verified.")
        return {"sub": info["sub"], "email": info["email"], "name": info.get("name") or ""}


def make_google_auth(settings: Settings):
    return GoogleAuth(settings)
