# Login and History API (for the Flutter app)

Every user has a session token. Before login it belongs to a **guest** (one guest per device); after Google/Apple login it belongs to the **account**.
Download history is stored on the server under that token, so it follows the account across devices.

```
App ──(encrypted token)──▶ Our backend ──▶ Auth service
App ◀──(encrypted user)─── Our backend ◀── Auth service
```

## 1. Flow

| When | Call | Then |
|------|------|------|
| App start, **no saved token** | `POST /auth/guest-login` | Save `data.token`. |
| App start, **token saved** | `POST /auth/auto-login` | `200`: keep going. `401`: delete the token and call guest login. |
| User taps Google / Apple | `POST /auth/google` or `/auth/apple`, **with the guest token** | Save the new `data.token` (it replaces the guest token). Reload history. |
| User taps Logout | `POST /auth/logout` | Delete the token, call guest login, reload history. |

**What happens to history:**

1. **Before login:** history is saved under the device's guest.
2. **Login:** the guest's history moves into the account and the guest is closed. Send the guest token in `Authorization`, or nothing moves.
3. **Logout, then guest login:** the device gets a **new, empty guest**. The old history belongs to the account now, so the guest can't see it.
4. **Login again:** the account's history is back, plus anything the new guest saved.

The session token goes in every call after guest login:

```
Authorization: Bearer <data.token>
```

## 2. Endpoints

`{{base_url}}` is the same base URL the app already uses for `/download_media`.

| Endpoint | Body | Encrypted? |
|----------|------|-----------|
| `POST /auth/guest-login` | `{"deviceId": "...", "deviceType": "android"}` | Yes |
| `POST /auth/google` | `{"token": "<idToken>", "deviceType": "android"}` | Yes |
| `POST /auth/apple` | `{"token": "<authorizationCode>", "deviceType": "ios"}` | Yes |
| `POST /auth/auto-login` | none | Yes (response) |
| `POST /auth/logout` | none | Yes (response) |
| `GET /history?limit=50` | none | Yes (answer) |
| `POST /history` | `{"url": "...", "item": {...}}` | Yes (body + answer) |
| `POST /history/delete` | `{"url": "..."}` | Yes (body + answer) |
| `POST /history/import` | `{"items": [{"url", "item"}, ...]}` (oldest first, max 200) | Yes (body + answer) |
| `DELETE /history` | none; `?scope=web` keeps app entries | Yes (answer) |

**Rule:** `/auth/*`, `/history...` and `/payment/...` are **always encrypted**: a request body is `encryptData(JSON)` sent as `text/plain` (a plain JSON body gets `400 Encrypted request required.`), requests without a body (GET / DELETE) need nothing, and every answer is encrypted, errors included. Only exception: `/payment/webhook/<key>` (called by the payment microservice) is plain JSON. The old APIs (`/download_media`, AI tools) follow `ENCRYPTION_REQUIRED` (see below).

| Field | Notes |
|-------|-------|
| `deviceId` | The same `deviceId` the app sends to `/download_media`. Required for guest login. |
| `deviceType` | `android`, `ios`, `web`, `mac`. `2` (Android) and `1` (iOS) also work. Optional. |
| `token` | Google: `idToken` from Google Sign-In. Apple: `authorizationCode` from Sign in with Apple (not the `identityToken`). |

Optional header `Lang` (e.g. `nl`) sets the language of login error messages.

## 3. Encryption (`/auth/*` only)

Both the request body and the response body are encrypted with **AES-256-GCM**, the same format the AiNoteTracker app uses.

- **Key:** `CLIENT_ENCRYPTION_KEY`. Get it from the backend team. Don't commit it in plain text.
- **Format:** `base64( IV (12 bytes) + tag (16 bytes) + ciphertext )`. The AES key is the SHA-256 of the key string.
- **Dart code:** copy `CryptoUtils` from AiNoteTracker (`lib/core/constants/crypto_utils.dart`) as it is. It needs the `pointycastle` package.
- **Request:** send the encrypted string as the body with `Content-Type: text/plain`. (A multipart field `encrypted_data` also works.) Plain requests get `400`.
- **Response:** always an encrypted string, for success and errors. Decrypt it, then `jsonDecode` it.

> One special case: if the server is missing its encryption key, the 500 response is **plain JSON**, not encrypted.
> So if decryption fails, try `jsonDecode` on the raw body.

## 4. Responses

**Guest login: HTTP 200**

```json
{
  "code": 200,
  "message": "Guest login successful.",
  "data": { "token": "Zk3...", "user_id": 41, "is_guest": true, "user": null }
}
```

Calling it again on the same device returns the same guest.

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

**History list: HTTP 200**

```json
{
  "code": 200,
  "message": "OK",
  "data": {
    "items": [
      {
        "url": "https://www.instagram.com/reel/abc/",
        "item": { "...": "whatever the app saved" },
        "created_at": "2026-10-29T11:40:02.512000",
        "updated_at": "2026-10-29T11:40:02.512000"
      }
    ]
  }
}
```

- **Order:** newest first. The server keeps the newest 200 items per user.
- **Same URL again:** saving a URL that is already there updates the item and moves it to the top.
- **`item`:** any JSON object up to 64 KB, stored as the app sends it. Store the post info (username, caption, thumbnail, post URL), not just the local file path. A `localPath` only exists on the phone that downloaded it, so check the file exists before opening it, and offer to download again if it doesn't.

**Errors.** Every error has this shape (decrypted, for `/auth/*`):

```json
{ "code": 401, "data": null, "message": "..." }
```

| HTTP | Meaning | What the app should do |
|------|---------|------------------------|
| 400 | `/auth/*` request could not be decrypted | Fix the app, because it's a bug. |
| 401 | Login: Google/Apple token rejected. Other calls: token missing, logged out or expired. | Login: ask the user to try again. Others: delete the token, call guest login. |
| 403 | Account action needed (e.g. verify email) | Show `message`. |
| 422 | A required field is missing or invalid | Show `message`. |
| 429 | Too many requests | Wait and retry. |
| 500 | Server or auth service problem | Show "Something went wrong. Please try again." |

## 5. Dart example (Dio)

```dart
const _tokenKey = 'session_token';
final crypto = CryptoUtils(ApiConstants.encryptionKey);

/// POST to /auth/*: encrypted body in, encrypted body out.
Future<(int, Map<String, dynamic>)> _authCall(String path, {Map<String, dynamic>? fields}) async {
  final prefs = await SharedPreferences.getInstance();
  final token = prefs.getString(_tokenKey);
  final response = await dio.post<String>(
    path,
    data: fields == null ? '' : crypto.encryptData(fields),
    options: Options(
      contentType: 'text/plain',
      responseType: ResponseType.plain,
      headers: {if (token != null) 'Authorization': 'Bearer $token'},
      validateStatus: (_) => true, // errors are encrypted too, so read every status
    ),
  );
  final raw = response.data ?? '';
  Map<String, dynamic> body;
  try {
    body = jsonDecode(crypto.decryptData(raw)) as Map<String, dynamic>;
  } catch (_) {
    body = jsonDecode(raw) as Map<String, dynamic>; // plain-JSON 500 case
  }
  return (response.statusCode ?? 500, body);
}

Future<void> _saveSession(Map<String, dynamic> body) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(_tokenKey, body['data']['token'] as String);
}

Future<void> guestLogin(String deviceId) async {
  final (status, body) = await _authCall('/auth/guest-login',
      fields: {'deviceId': deviceId, 'deviceType': Platform.isIOS ? 'ios' : 'android'});
  if (status != 200) throw Exception(body['message']);
  await _saveSession(body);
}

/// Call on app start.
Future<void> startSession(String deviceId) async {
  final prefs = await SharedPreferences.getInstance();
  if (prefs.getString(_tokenKey) != null) {
    final (status, _) = await _authCall('/auth/auto-login');
    if (status == 200) return;
    if (status == 401) await prefs.remove(_tokenKey); // logged out or expired
    else return; // server problem: keep the token and try again later
  }
  await guestLogin(deviceId);
}

/// provider: 'google' or 'apple'. Sends the guest token so guest history moves to the account.
Future<Map<String, dynamic>> socialLogin(String provider, String providerToken) async {
  final (status, body) = await _authCall('/auth/$provider',
      fields: {'token': providerToken, 'deviceType': Platform.isIOS ? 'ios' : 'android'});
  if (status != 200) throw Exception(body['message'] ?? 'Login failed');
  await _saveSession(body);
  return body['data']['user'] as Map<String, dynamic>;
}

Future<void> logout(String deviceId) async {
  await _authCall('/auth/logout');
  final prefs = await SharedPreferences.getInstance();
  await prefs.remove(_tokenKey);
  await guestLogin(deviceId); // new, empty guest
}

/// History calls are plain JSON with the same Authorization header.
Future<List<dynamic>> fetchHistory() async {
  final prefs = await SharedPreferences.getInstance();
  final response = await dio.get('/history',
      options: Options(headers: {'Authorization': 'Bearer ${prefs.getString(_tokenKey)}'}));
  return response.data['data']['items'] as List<dynamic>;
}

Future<void> addToHistory(String postUrl, Map<String, dynamic> item) async {
  final prefs = await SharedPreferences.getInstance();
  await dio.post('/history',
      data: {'url': postUrl, 'item': item},
      options: Options(headers: {'Authorization': 'Bearer ${prefs.getString(_tokenKey)}'}));
}
```

**Getting the provider token**

```dart
// Google (google_sign_in)
final account = await GoogleSignIn().signIn();
final idToken = (await account!.authentication).idToken!;
await socialLogin('google', idToken);

// Apple (sign_in_with_apple)
final credential = await SignInWithApple.getAppleIDCredential(
  scopes: [AppleIDAuthorizationScopes.email, AppleIDAuthorizationScopes.fullName],
);
await socialLogin('apple', credential.authorizationCode); // not identityToken
```

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

## 6. History in the Flutter app (as built)

The app shows download history **from the phone, per account** (its items are files on that phone), and keeps a **copy on the backend** under the current guest / account.

**On the phone**
- `savedPosts` is always the current user's list (guest or account), so the existing screens keep working.
- **Login:** the account's saved list on this phone (`savedPosts_account_<user_id>`) + what the guest saved becomes `savedPosts`.
- **Logout:** `savedPosts` is moved to `savedPosts_account_<user_id>` and a new, empty guest starts.

**On the backend** (`lib/services/history_sync_service.dart`)
- **Once after updating** (today's live users have no login): right after the first guest login, the whole local list (newest 200) goes to `POST /history/import`, so it is bound to the guest. It is marked done only when the server answers 200, otherwise it retries on the next launch.
- **Then:** every new download goes to `POST /history`, every delete to `POST /history/delete`.
- **Login:** the backend moves the guest's entries to the account (like on web).
- Server entry: `url` = `<postUrl>#<fileName>` (or `app-file:<fileName>` without a post URL), `item` = `{"source": "app", "platform": "ios|android", "fileName", "postUrl", "username", "caption"}`. No device file path.
- The web app skips `source: "app"` entries (no media to show), and its Clear button uses `DELETE /history?scope=web`, which keeps them.

On iOS the native app's history is first imported into `savedPosts` (`ios/Runner/NativeAppMigration.swift`), so it is uploaded the same way.

Implementation: `lib/services/auth_service.dart` (session + history swap), `lib/services/history_sync_service.dart` (backend copy), `lib/widgets/account_section.dart` (Settings), `lib/services/app_config.dart` (build settings).

## 7. Checklist

- [ ] `CryptoUtils` added and `CLIENT_ENCRYPTION_KEY` set
- [ ] App start: auto login if a token is saved, otherwise guest login
- [ ] Guest token sent in `Authorization` when calling Google/Apple login
- [ ] New token saved after login; token deleted and guest login called after logout
- [ ] `401` on any call: delete the token and call guest login
- [ ] History swapped per account on login / logout, local history uploaded once (§6)
- [ ] Google login sends the `idToken`, not the `accessToken`; Apple sends the `authorizationCode`
