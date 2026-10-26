"""Sign-in with Supabase Auth (email + password with email verification and password reset, or Google).

The browser signs in with supabase-js and sends its access token once; the server asks Supabase who the token
belongs to, then opens its own session (cookie). Only verified email addresses are accepted.
"""

import httpx

from .config import Settings


class AuthError(RuntimeError):
    pass


class SupabaseAuth:
    def __init__(self, settings: Settings):
        if not (settings.supabase_url and settings.supabase_anon_key):
            raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY are required for sign-in")
        self._http = httpx.Client(base_url=settings.supabase_url, headers={"apikey": settings.supabase_anon_key},
                                  timeout=15)

    def verify(self, access_token: str) -> dict:
        """Return {"sub", "email", "name"} for a valid, verified Supabase session, else raise AuthError."""
        try:
            resp = self._http.get("/auth/v1/user", headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not reach the sign-in service: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError("Your sign-in has expired. Please sign in again.")
        user = resp.json()
        if not user.get("email") or not user.get("email_confirmed_at"):
            raise AuthError("Please confirm your email address first — check your inbox for the link.")
        meta = user.get("user_metadata") or {}
        return {"sub": user["id"], "email": user["email"],
                "name": meta.get("full_name") or meta.get("name") or ""}


def make_auth(settings: Settings):
    return SupabaseAuth(settings)
