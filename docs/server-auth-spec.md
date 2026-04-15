# Sayzo Server Auth Spec — Context for Implementation

> **What this is:** A spec for the auth endpoints that the Sayzo desktop agent (Python CLI) expects. Drop this into the Claude session working on your Next.js app so it implements the correct server-side routes.

## Background

Sayzo has a **local Python agent** that runs on the user's machine, captures English conversations, and uploads transcripts + audio to the server. The agent authenticates via **OAuth 2.0 PKCE** (browser-based login). The server needs to act as a thin OAuth layer backed by **Firebase Auth with Google provider**.

The agent is already implemented. The server endpoints below are what the agent calls. Do not change the endpoint contracts — implement them exactly as specified.

## Stack

- **Next.js** (App Router or Pages Router — your choice)
- **Firebase Auth** — Google provider for user sign-in
- **Firebase Admin SDK** (`firebase-admin`) — for verifying tokens server-side
- **Firestore** — for storing authorization codes and any user metadata

## Auth Flow (step by step)

```
Python CLI                          Next.js Server                    Firebase/Google
─────────                          ──────────────                    ──────────────
1. Opens browser to
   /api/auth/authorize?
     redirect_uri=http://127.0.0.1:PORT/callback
     &code_challenge=XXXX
     &code_challenge_method=S256
     &state=YYYY
                          ──────►
                                   2. Stores code_challenge + state
                                      in a short-lived Firestore doc
                                      (or in-memory/Redis, TTL ~5 min)
                                   3. Redirects to a login page OR
                                      directly to Google OAuth
                                                                ──────►
                                                                   4. User signs in
                                                                      with Google
                                                                ◄──────
                                   5. Firebase Auth callback fires,
                                      server gets Firebase ID token
                                   6. Generates a random auth code,
                                      stores it with:
                                      - code_challenge from step 2
                                      - Firebase UID
                                      - redirect_uri
                                      - expires_at (5 min from now)
                                   7. Redirects browser to:
                                      {redirect_uri}?code={auth_code}&state={state}
                          ◄──────
8. CLI receives code
   on localhost

9. CLI calls POST
   /api/auth/token
   {grant_type: "authorization_code",
    code: AUTH_CODE,
    code_verifier: VERIFIER,
    redirect_uri: ...}
                          ──────►
                                   10. Looks up the auth code in Firestore
                                   11. Verifies: SHA256(code_verifier) == stored code_challenge
                                   12. Verifies: redirect_uri matches
                                   13. Verifies: not expired
                                   14. Deletes the auth code doc (single use)
                                   15. Mints tokens:
                                       - access_token: a signed JWT (your own, or Firebase custom token)
                                       - refresh_token: a random opaque string stored in Firestore
                                       - expires_in: 3600 (1 hour)
                                   16. Returns token response
                          ◄──────
17. CLI stores tokens
    locally in auth.json
```

## Endpoints to Implement

### `GET /api/auth/authorize`

**Purpose:** Start the login flow. The CLI opens the user's browser to this URL.

**Query parameters:**
| Param | Required | Description |
|---|---|---|
| `redirect_uri` | Yes | Always `http://127.0.0.1:{PORT}/callback` (localhost) |
| `code_challenge` | Yes | S256 PKCE challenge (base64url-encoded SHA256 of verifier) |
| `code_challenge_method` | Yes | Always `S256` |
| `state` | Yes | Random string for CSRF protection |
| `scope` | No | `offline_access upload` (can ignore for now) |
| `client_id` | No | Public client ID (validate if you want, not critical for PKCE) |

**Behavior:**
1. Store `code_challenge`, `state`, and `redirect_uri` in Firestore with a 5-minute TTL. Use a random session ID as the document key.
2. Set the session ID in a short-lived cookie (or pass via `state` param to Google OAuth).
3. Redirect to your login page where the user clicks "Sign in with Google", OR redirect directly to Google OAuth via Firebase Auth.
4. After successful Firebase Auth sign-in, generate a random authorization code (e.g., `crypto.randomUUID()`), store it in Firestore linked to the session + Firebase UID, then redirect to `{redirect_uri}?code={auth_code}&state={state}`.

**Important:** Validate that `redirect_uri` starts with `http://127.0.0.1` or `http://localhost` — reject anything else. This prevents open redirect attacks.

### `POST /api/auth/token`

**Purpose:** Exchange an authorization code for tokens, or refresh an expired access token.

**Content-Type:** `application/x-www-form-urlencoded`

**Request body (code exchange):**
```
grant_type=authorization_code
code=THE_AUTH_CODE
code_verifier=THE_PKCE_VERIFIER
redirect_uri=http://127.0.0.1:PORT/callback
client_id=OPTIONAL
```

**Request body (refresh):**
```
grant_type=refresh_token
refresh_token=THE_REFRESH_TOKEN
client_id=OPTIONAL
```

**Success response (200):**
```json
{
  "access_token": "eyJ...",
  "refresh_token": "dGhp...",
  "expires_in": 3600,
  "token_type": "Bearer"
}
```

**Error response (400/401):**
```json
{
  "error": "invalid_grant",
  "error_description": "Authorization code expired"
}
```

**Code exchange behavior:**
1. Look up the authorization code in Firestore. If not found or expired → 400 `invalid_grant`.
2. Compute `SHA256(code_verifier)`, base64url-encode it, compare to stored `code_challenge`. Mismatch → 400 `invalid_grant`.
3. Verify `redirect_uri` matches what was stored. Mismatch → 400 `invalid_grant`.
4. Delete the authorization code doc (single-use).
5. Mint an **access token**: sign a JWT with the Firebase UID, email, and an expiry (1 hour). Use a server-side secret or Firebase Admin SDK's `createCustomToken()`. The access token is what the agent sends with upload requests.
6. Mint a **refresh token**: generate a random opaque string (e.g., 64 hex chars), store it in Firestore linked to the Firebase UID, with a long expiry (90 days).
7. Return the token response.

**Refresh behavior:**
1. Look up the refresh token in Firestore. Not found or expired → 401.
2. Mint a new access token (same as above).
3. Optionally rotate the refresh token (mint a new one, delete the old one).
4. Return the token response.

### Upload Endpoints (future, not yet needed)

When you implement the actual upload, the agent will call something like:

```
POST /api/captures/upload
Authorization: Bearer {access_token}
Content-Type: multipart/form-data

- record.json (the transcript + metadata)
- audio.opus (stereo Opus file, L=mic R=system)
```

Your server middleware should:
1. Extract the Bearer token from the `Authorization` header.
2. Verify the JWT (check signature, expiry, extract Firebase UID).
3. Use the UID to associate the upload with the right user in Firestore.

This is not implemented yet on either side — just keep it in mind when designing the token payload.

## Access Token JWT Payload

The access token JWT should contain at minimum:

```json
{
  "sub": "firebase-uid-here",
  "email": "user@example.com",
  "iat": 1712800000,
  "exp": 1712803600
}
```

Sign it with a server-side secret (e.g., `jose` / `jsonwebtoken` library). The agent doesn't decode the JWT — it just passes it as a Bearer token. Your server verifies it on protected routes.

## Firestore Collections

Suggested schema (adjust as needed):

```
auth_sessions/{sessionId}
  - code_challenge: string
  - code_challenge_method: "S256"
  - redirect_uri: string
  - state: string
  - created_at: timestamp
  - expires_at: timestamp (now + 5 min)

auth_codes/{code}
  - session_id: string
  - firebase_uid: string
  - redirect_uri: string
  - created_at: timestamp
  - expires_at: timestamp (now + 5 min)

refresh_tokens/{token}
  - firebase_uid: string
  - created_at: timestamp
  - expires_at: timestamp (now + 90 days)
```

Clean up expired docs with a Firestore TTL policy or a scheduled Cloud Function.

## File Structure Suggestion

```
app/
  api/
    auth/
      authorize/
        route.ts       ← GET handler
      token/
        route.ts       ← POST handler
      callback/
        route.ts       ← Firebase Auth callback (after Google sign-in)
  login/
    page.tsx           ← Login page with "Sign in with Google" button
lib/
  auth/
    jwt.ts             ← sign/verify access tokens
    pkce.ts            ← PKCE verification helper (SHA256 + base64url compare)
    firestore.ts       ← auth_sessions, auth_codes, refresh_tokens CRUD
```

## Config the CLI Expects

Once implemented, the Python agent is configured with:
```
SAYZO_AUTH__AUTH_URL=https://your-domain.com/api/auth
SAYZO_AUTH__CLIENT_ID=sayzo-desktop
```

The CLI appends `/authorize` and `/token` to `AUTH_URL`. So if your routes are at `/api/auth/authorize` and `/api/auth/token`, set AUTH_URL to `https://your-domain.com/api/auth`.

## Security Checklist

- [ ] `redirect_uri` must start with `http://127.0.0.1` or `http://localhost` — reject all others
- [ ] Authorization codes are single-use — delete after exchange
- [ ] Authorization codes expire in 5 minutes
- [ ] PKCE verification is mandatory — never skip the `code_verifier` check
- [ ] Refresh tokens are stored hashed (optional but good practice)
- [ ] Access token JWTs are signed with a secret that isn't exposed client-side
- [ ] All Firestore auth docs have TTL-based cleanup
- [ ] Rate-limit `/api/auth/token` to prevent brute-force

## Testing

To test end-to-end:
1. Start your Next.js dev server
2. On the Python agent side, set:
   ```
   SAYZO_AUTH__AUTH_URL=http://localhost:3000/api/auth
   SAYZO_AUTH__CLIENT_ID=sayzo-desktop
   ```
3. Run `sayzo-agent login`
4. Browser should open, you sign in with Google, browser redirects to localhost, CLI prints "Login successful."
5. Check `sayzo-data/auth.json` — should contain access_token, refresh_token, expires_at.
