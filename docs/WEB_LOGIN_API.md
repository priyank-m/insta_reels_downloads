# Login and History API (for the React web app)

Same API as the mobile apps (see `FRONTEND_LOGIN_API.md`), written for the React + Vite web app (`insta_save/React_web`).

Every visitor has a session token. Before login it belongs to a **guest** (one guest per browser, using the existing `instasave_device_id`); after Google/Apple login it belongs to the **account**.
Download history is stored on the server under that token, so it follows the account to the phone apps too.

```
Browser ──(encrypted token)──▶ /api proxy ──▶ Our backend ──▶ Auth service
Browser ◀──(encrypted user)─── /api proxy ◀── Our backend ◀── Auth service
```

## 1. Flow

| When | Call | Then |
|------|------|------|
| Page load, **no saved token** | `POST /auth/guest-login` | Save `data.token`. |
| Page load, **token saved** | `POST /auth/auto-login` | `200`: keep going. `401`: delete the token and call guest login. |
| User clicks Google / Apple | `POST /auth/google` or `/auth/apple`, **with the guest token** | Save the new `data.token` (it replaces the guest token). Reload history. |
| User clicks Logout | `POST /auth/logout` | Delete the token, call guest login, reload history. |

**What happens to history:**

1. **Before login:** history is saved under the browser's guest.
2. **Login:** the guest's history moves into the account and the guest is closed. Send the guest token in `Authorization`, or nothing moves.
3. **Logout, then guest login:** the browser gets a **new, empty guest**. The old history belongs to the account now, so the guest can't see it.
4. **Login again:** the account's history is back, plus anything the new guest saved.

The session token goes in every call after guest login:

```
Authorization: Bearer <data.token>
```

## 2. Endpoints

Call them through the existing `/api` proxy, the same way as `/download_media`. The backend sends no CORS headers.
`API_BASE` is the one in `src/services/api.js` (`VITE_API_BASE` or `/api`).
`vite.config.js` (dev) and `vercel.json` (production) already forward `/api/*` to the backend, so no proxy change is needed.

| Endpoint | Body | Encrypted? |
|----------|------|-----------|
| `POST ${API_BASE}/auth/guest-login` | `{"deviceId": "<instasave_device_id>", "deviceType": "web"}` | Yes |
| `POST ${API_BASE}/auth/google` | `{"token": "<credential>", "deviceType": "web"}` | Yes |
| `POST ${API_BASE}/auth/apple` | `{"token": "<authorization code>", "deviceType": "web"}` | Yes |
| `POST ${API_BASE}/auth/auto-login` | none | Yes (response) |
| `POST ${API_BASE}/auth/logout` | none | Yes (response) |
| `GET ${API_BASE}/history?limit=50` | none | Yes (answer) |
| `POST ${API_BASE}/history` | `{"url": "...", "item": {...}}` | Yes (body + answer) |
| `POST ${API_BASE}/history/delete` | `{"url": "..."}` | Yes (body + answer) |
| `DELETE ${API_BASE}/history?scope=web` | none (clears the site's entries, keeps the phone's) | Yes (answer) |

**Rule:** `/auth/*`, `/history...` and `/payment/...` are **always encrypted**: a request body is `encryptData(JSON)` sent as `text/plain` (a plain JSON body gets `400 Encrypted request required.`), requests without a body (GET / DELETE) need nothing, and every answer is encrypted, errors included. Only exception: `/payment/webhook/<key>` (called by the payment microservice) is plain JSON. The old APIs (`/download_media`, AI tools) follow `ENCRYPTION_REQUIRED` (see below).

Optional header `Lang` (e.g. `nl`) sets the language of login error messages.

## 3. Encryption (`/auth/*` only)

Both the request body and the response body are encrypted with **AES-256-GCM**, the same format as the mobile apps.

- **Key:** `CLIENT_ENCRYPTION_KEY`. Get it from the backend team and put it in `.env` as `VITE_CLIENT_ENCRYPTION_KEY`.
- **Format:** `base64( IV (12 bytes) + tag (16 bytes) + ciphertext )`. The AES key is the SHA-256 of the key string.
- **Library:** the browser's built-in **Web Crypto API**, so no extra package is needed.

> Anything in a `VITE_` variable ends up in the public JS bundle, so anyone can read this key.
> The encryption only hides the payload from casual inspection; HTTPS is what protects it in transit.
> Never put the auth service key or the `App` key in the web app.

Add `src/services/clientCrypto.js`:

```js
// AES-256-GCM, same format as the backend and the Flutter CryptoUtils:
// base64( iv(12) | tag(16) | ciphertext ), key = SHA-256(key string).
const KEY_STRING = import.meta.env.VITE_CLIENT_ENCRYPTION_KEY;
const encoder = new TextEncoder();
const decoder = new TextDecoder();
let keyPromise;

function getKey() {
  if (!KEY_STRING) throw new Error('VITE_CLIENT_ENCRYPTION_KEY is not set');
  keyPromise ??= crypto.subtle
    .digest('SHA-256', encoder.encode(KEY_STRING))
    .then((raw) => crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']));
  return keyPromise;
}

const toBase64 = (bytes) => btoa(String.fromCharCode(...bytes));
const fromBase64 = (text) => Uint8Array.from(atob(text.trim()), (c) => c.charCodeAt(0));

export async function encryptData(data) {
  const text = typeof data === 'string' ? data : JSON.stringify(data);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  // Web Crypto returns ciphertext + tag; the backend wants iv + tag + ciphertext.
  const sealed = new Uint8Array(
    await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, await getKey(), encoder.encode(text)),
  );
  const ciphertext = sealed.slice(0, -16);
  const tag = sealed.slice(-16);
  return toBase64(new Uint8Array([...iv, ...tag, ...ciphertext]));
}

export async function decryptData(encrypted) {
  const data = fromBase64(encrypted);
  const iv = data.slice(0, 12);
  const tag = data.slice(12, 28);
  const ciphertext = data.slice(28);
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv },
    await getKey(),
    new Uint8Array([...ciphertext, ...tag]),
  );
  return decoder.decode(plain);
}
```

Web Crypto only works on HTTPS pages and `localhost`. An `http://` LAN address won't have `crypto.subtle`.

## 4. Responses

`/auth/*` responses are always an encrypted string, for success and errors. Decrypt, then `JSON.parse`.

**Guest login: HTTP 200**

```json
{
  "code": 200,
  "message": "Guest login successful.",
  "data": { "token": "Zk3...", "user_id": 41, "is_guest": true, "user": null }
}
```

Calling it again with the same `deviceId` returns the same guest.

**Google / Apple login: HTTP 200**

```json
{
  "message": "Logged In successfully.",
  "code": 200,
  "data": {
    "token": "q9P...",
    "user_id": 42,
    "is_guest": false,
    "user": {
      "id": 6,
      "name": "Test User",
      "email": "user@example.com",
      "phone_number": null,
      "auth_key": "aa2d072d60848f890eff0f4909c08020",
      "email_verified": true
    }
  },
  "extra": null
}
```

**Auto login: HTTP 200.** Same `data` shape: `{token, user_id, is_guest, user}`. The token stays the same.

**Logout: HTTP 200.** `{"code": 200, "data": null, "message": "Logged out successfully."}`

**History list: HTTP 200** (plain JSON)

```json
{
  "code": 200,
  "message": "OK",
  "data": {
    "items": [
      {
        "url": "https://www.instagram.com/reel/abc/",
        "item": { "originalUrl": "https://www.instagram.com/reel/abc/", "...": "the post data the site saved" },
        "created_at": "2026-10-29T11:40:02.512000",
        "updated_at": "2026-10-29T11:40:02.512000"
      }
    ]
  }
}
```

- **Order:** newest first. The server keeps the newest 200 items per user.
- **Same URL again:** saving a URL that is already there updates the item and moves it to the top, the same as today's `filter(originalUrl)` logic.
- **`item`:** any JSON object up to 64 KB. Save the same `data` object the site puts in `instasaver_history` today, with `url` = `data.originalUrl`.
- **Phone entries:** the mobile app also stores its downloads here, with `item.source = "app"` and no `media`. The site skips them (`fetchHistory` filters them out) and clears with `?scope=web` so they stay.

**Errors.** Every error has this shape (decrypted, for `/auth/*`):

```json
{ "code": 401, "data": null, "message": "..." }
```

| HTTP | Meaning | What the site should do |
|------|---------|-------------------------|
| 400 | `/auth/*` request could not be decrypted (wrong key, plain body, or broken data) | Fix the site, because it's a bug. |
| 401 | Login: Google/Apple token rejected. Other calls: token missing, logged out or expired. | Login: ask the user to try again. Others: delete the token, call guest login. |
| 403 | Account action needed (e.g. verify email) | Show `message`. |
| 422 | A required field is missing or invalid | Show `message`. |
| 429 | Too many requests | Wait and retry. |
| 500 | Server or auth service problem | Show "Something went wrong. Please try again." |

> One special case: if the server is missing its encryption key, the 500 response is **plain JSON**, not encrypted.
> Also, a proxy or Vercel error page is not encrypted. So if decryption fails, fall back to `JSON.parse`, and then to a generic error.

## 5. Session code

Replace the `auth_key` storage in `src/services/auth.js` with the session token:

```js
import { encryptData, decryptData } from './clientCrypto';

const TOKEN_STORAGE = 'instasave_session_token';
const GENERIC_ERROR = 'Something went wrong. Please try again.';

const getToken = () => { try { return localStorage.getItem(TOKEN_STORAGE); } catch { return null; } };
const setToken = (t) => { try { localStorage.setItem(TOKEN_STORAGE, t); } catch { /* private mode */ } };
const dropToken = () => { try { localStorage.removeItem(TOKEN_STORAGE); } catch { /* private mode */ } };
const authHeader = () => (getToken() ? { Authorization: `Bearer ${getToken()}` } : {});

/** POST to /auth/*: encrypted body in, encrypted body out. Returns { status, body }. */
async function authCall(path, fields) {
  const response = await fetch(`${API_BASE}/auth/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain', ...authHeader() },
    body: fields ? await encryptData(fields) : '',
  });
  const raw = await response.text();
  let body;
  try {
    body = JSON.parse(await decryptData(raw));
  } catch {
    try { body = JSON.parse(raw); } catch { body = { code: response.status, message: GENERIC_ERROR }; }
  }
  return { status: response.status, body };
}

export async function guestLogin() {
  const { status, body } = await authCall('guest-login', { deviceId: getDeviceId(), deviceType: 'web' });
  if (status !== 200) throw new Error(body.message || GENERIC_ERROR);
  setToken(body.data.token);
  return body.data;
}

/** Call once on page load. Resolves with { user_id, is_guest, user }. */
export async function startSession() {
  if (getToken()) {
    const { status, body } = await authCall('auto-login');
    if (status === 200) return body.data;
    if (status !== 401) throw new Error(body.message || GENERIC_ERROR); // server problem: keep the token
    dropToken(); // logged out or expired
  }
  return guestLogin();
}

/** provider: 'google' | 'apple'. Sends the guest token so guest history moves to the account. */
export async function socialLogin(provider, providerToken) {
  const { status, body } = await authCall(provider, { token: providerToken, deviceType: 'web' });
  if (status !== 200) throw new Error(body.message || 'Login failed. Please try again.');
  setToken(body.data.token);
  return body.data.user;
}

export async function logout() {
  await authCall('logout').catch(() => {});
  dropToken();
  window.google?.accounts?.id?.disableAutoSelect?.();
  return guestLogin(); // new, empty guest
}

// ── History (plain JSON) ─────────────────────────────────────
export async function fetchHistory() {
  const response = await fetch(`${API_BASE}/history?limit=30`, { headers: authHeader() });
  if (!response.ok) throw new Error(GENERIC_ERROR);
  return (await response.json()).data.items.map((entry) => entry.item);
}

export async function addToHistory(data) {
  await fetch(`${API_BASE}/history`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify({ url: data.originalUrl, item: data }),
  });
}

export async function removeFromHistory(url) {
  await fetch(`${API_BASE}/history/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify({ url }),
  });
}

export async function clearHistory() {
  await fetch(`${API_BASE}/history`, { method: 'DELETE', headers: authHeader() });
}
```

`getDeviceId` is the existing helper in `src/services/api.js` (export it).

In `App.jsx`:

- **Page load:** `await startSession()`, then `setHistory(await fetchHistory())`.
- **After a fetch:** call `addToHistory(data)` next to the existing `setHistory(...)`.
- **Clear button:** call `clearHistory()`.
- **After login and logout:** reload with `setHistory(await fetchHistory())`.
- **Stop saving to `instasaver_history`.** It would show the account's history to the next guest in the same browser.

## 6. Moving existing local history

Today history lives only in `localStorage` (`instasaver_history`). Once, after the first guest login on the updated site, send each saved item with `addToHistory(item)` (oldest first, so the newest ends on top), then remove `instasaver_history`.

## Old APIs: optional encryption

`/download_media`, `/frontend_success`, `/transcribe`, `/extract_hook`, `/trendy_captions`, `/trendy_hashtags`, `/groq_caption`, `/groq_hashtags` accept encrypted requests (same key and format as `/auth/*`). The backend setting `ENCRYPTION_REQUIRED` decides:

| `ENCRYPTION_REQUIRED` | Plain request | Encrypted request |
|---|---|---|
| `0` (now) | works as before | works |
| `1` | `400 {"code": 400, "message": "Encrypted request required."}` | works |

How to encrypt:
- **With a file** (`/transcribe`, `/extract_hook`, `/trendy_captions`, `/trendy_hashtags`): multipart as today, but put the text fields in one field `encrypted_data` = `encryptData({"caption": "...", "niche": "..."})`. The `video_file` part stays plain.
- **Without a file** (`/download_media`, `/frontend_success`, `/groq_caption`, `/groq_hashtags`): body = `encryptData({"instagramURL": "...", "deviceId": "...", "deviceType": 2})`, `Content-Type: text/plain`. (An `encrypted_data` form field also works.)
- **Response:** an encrypted request gets an encrypted `text/plain` response, errors included: decrypt, then parse JSON.

Switch to `1` only after every app version in use encrypts these calls, or old installed apps stop working.

## 7. Getting the provider token

### Google: Google Identity Services

Load the script in `index.html`:

```html
<script src="https://accounts.google.com/gsi/client" async></script>
```

Render the button and send the `credential` (it is the ID token):

```js
window.google.accounts.id.initialize({
  client_id: import.meta.env.VITE_GOOGLE_WEB_CLIENT_ID,
  callback: async ({ credential }) => {
    const user = await socialLogin('google', credential);
    // logged in
  },
});
window.google.accounts.id.renderButton(document.getElementById('google-login'), {
  theme: 'outline',
  size: 'large',
});
```

- **Client ID:** use a **Web** OAuth client ID. Add the site's domains (and `http://localhost:3000`) to its *Authorized JavaScript origins*.
- **Token type:** send the `credential`, not an OAuth access token.

### Apple: Sign in with Apple JS

Load the script in `index.html`:

```html
<script src="https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js"></script>
```

```js
window.AppleID.auth.init({
  clientId: import.meta.env.VITE_APPLE_SERVICES_ID, // Services ID, not the iOS bundle ID
  scope: 'name email',
  redirectURI: window.location.origin + '/auth/apple-callback', // must be registered with Apple
  usePopup: true,
});

async function appleLogin() {
  const { authorization } = await window.AppleID.auth.signIn();
  // Send the authorization code, not the id_token: the auth service exchanges it with Apple
  const user = await socialLogin('apple', authorization.code);
  // logged in
}
```

- **Setup:** Apple web login needs a **Services ID** in the Apple Developer account, with the site's domain and return URL registered. Localhost is not allowed, so test it on a real HTTPS domain or an ngrok tunnel.
- **Auth service:** the auth service must accept tokens issued for the **web Google client ID** and the **Apple Services ID**. Confirm with the auth team before testing, otherwise every web login fails with an error.

## 8. Checklist

- [ ] `VITE_CLIENT_ENCRYPTION_KEY`, `VITE_GOOGLE_WEB_CLIENT_ID`, `VITE_APPLE_SERVICES_ID` set (locally and in Vercel)
- [ ] Auth team confirmed the web Google client ID and Apple Services ID are allowed
- [ ] `clientCrypto.js` added; works on `localhost` and the HTTPS domain
- [ ] Requests go through `${API_BASE}` (the `/api` proxy), not straight to the backend
- [ ] Page load: auto login if a token is saved, otherwise guest login
- [ ] Guest token sent in `Authorization` when calling Google/Apple login
- [ ] New token saved after login; token deleted and guest login called after logout
- [ ] `401` on any call: delete the token and call guest login
- [ ] History read from `GET /history`; new fetches saved with `POST /history`; `instasaver_history` no longer used
- [ ] Old `instasaver_history` uploaded once
