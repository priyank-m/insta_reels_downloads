# Project Engineering Instructions

## 1. Project Overview

Insta Save is a Python 3.9 Dockerized FastAPI backend for a mobile/web client. Its primary operation accepts an Instagram (or supported Threads) URL and a device identifier, resolves publicly accessible media through several third-party providers, and returns direct media URLs with available creator metadata. It also exposes AI-assisted caption, hashtag, transcription, and hook-extraction endpoints for uploaded media, plus IP-restricted secret-encryption endpoints.

The runtime is synchronous work inside `async` FastAPI handlers. `python -m app` starts Uvicorn; the Docker entrypoint additionally starts Tor and `python -m api.scheduler` for Apify quota rotation. MySQL is both configuration/state storage and analytics storage. External boundaries include Instagram, provider sites/APIs, Apify, Tor, Selenium/Chromium, Gemini, and Groq.

Key dependencies are FastAPI/Uvicorn, `requests`, Instaloader, yt-dlp, Selenium and webdriver-manager, MySQL Connector, Stem/Tor, Tenacity, Google GenAI, Cryptography/Fernet, and `schedule`. Dependencies are listed only in `requirements.txt`; there is no pyproject, lockfile, lint configuration, CI workflow, or committed automated test suite.

## 2. Repository Architecture

### Application and API

- `app/main.py` creates the FastAPI application, includes `app.api.v1.router.api_router`, registers a catch-all 500 handler, and registers a startup hook that seeds environment secrets into MySQL. `app/__main__.py` runs Uvicorn using `settings.host` and `settings.port`. `api/__main__.py` is an equivalent legacy launcher; `api.__getattr__('app')` preserves a legacy app import.
- `app/api/v1/endpoints/instagram.py` is the HTTP boundary. It defines unprefixed multipart POST routes: `/download_media`, `/frontend_success`, `/trendy_captions`, `/trendy_hashtags`, `/groq_caption`, `/groq_hashtags`, `/transcribe`, and `/extract_hook`. It delegates without business logic to `app.services.instagram_service`.
- `app/api/v1/endpoints/health.py` exposes unauthenticated `GET /api/health`. It delegates response construction to `app.services.health_service`, returning HTTP 200 only when MySQL accepts `SELECT 1`, otherwise HTTP 503. Its response intentionally contains app name/environment, per-check status and elapsed milliseconds, and a UTC timestamp; it must never return database credentials or raw connection errors.
- `app/api/v1/endpoints/crypto.py` exposes JSON `/crypto/encrypt` and `/crypto/decrypt`. The entire router uses `require_crypto_allowed_ip`; response shape is `ApiResponse` (`code`, optional `data`, optional `message`). `ip_allowlist.py` accepts either the direct client IP or the first `X-Forwarded-For` IP, so proxy deployment must ensure that header cannot be spoofed by untrusted clients.
- `app/exceptions/handlers.py` turns unhandled exceptions into `{"code": 500, "data": null, "message": ...}`. Many service paths instead catch their own errors and return their established response dictionaries.

### Configuration, database, and secrets

- `app/core/config.py` loads `.env` at import time and caches a Pydantic `Settings`. Configuration includes host/port, MySQL connection values, encryption key, crypto allowlist, Tor password, scheduler log/interval, and RapidAPI host/key. Changing environment variables after import will not change the singleton without process restart/cache reset.
- `app/db/session.py:get_connection` opens a new MySQL connection per operation and returns `None` after logging connector errors. There is no ORM or application-owned migration framework; repositories lazily create/alter some tables.
- `app/core/crypto.py` prefixes encrypted values with `enc:` and encrypts with Fernet. A non-Fernet `APP_ENCRYPTION_KEY` is deterministically SHA-256-derived to a Fernet key for backward compatibility. Never expose plaintext secrets, encrypted tokens, or the encryption key in logs or responses.
- `app/repositories/settings_repository.py` stores settings in `my_settings`. It selects `production_value` only for `APP_ENV` values `prod`/`production`, otherwise `development_value`; secret environment values are seeded only when absent unless forced. `scripts/migrate-secrets.py` migrates/verifies secrets, and `scripts/set-encrypted-setting.py` interactively or explicitly stores one plaintext value encrypted.
- `app/repositories/apify_key_repository.py` incrementally adds `apify_keys.token_encrypted`, encrypts any legacy plaintext `token`, then clears that plaintext column. Call `ensure_apify_token_encryption` before relying on an encrypted token.
- `app/repositories/instagram_service_repository.py` manages `download_media_service_config`. Although its API receives a context, it currently overwrites it with `all`; it seeds default services, optionally copies legacy `post` settings into `all`, and merges database sort/order/enabled flags with the in-code service definitions. This is shared mutable configuration for all post/profile fallback paths.

### Instagram service and state

`app/services/instagram_service.py` is the integration layer and contains both provider parsers and all Instagram/AI business logic. It produces provider results shaped as:

```python
{
    "postData": [{"type": "GraphImage" | "GraphVideo", "thumbnail": str, "link": str}],
    "username": str,
    "profilePic": str,
    "caption": str,
    # optional: "hashtags": list[str]
}
```

Important sections are:

- URL/metadata helpers: `normalize_instagram_url`, `_InstagramMetaParser`, `fetch_instagram_page_metadata`, `fetch_instagram_og_metadata`, `enrich_instagram_metadata`, and photo/video/Threads predicates.
- Provider adapters/parsers: `fetch_instagram_rapidapi_provider`, DownloadGram (`fetch_instagram_downloadgram`), GrabGram (`fetch_instagram_snapdownloader`), SnapInsta Selenium interception (`fetch_instagram_data`), `fetch_instagram_oembed_post`, yt-dlp, Instaloader, GlobalSource, SSSInstagram, `fetch_apify_instagram_post`, an indown/GraphQL proxy chain, and SaveClip through ProxyOrb. DownloadGram posts form data directly to `api.downloadgram.org/media`, parses its escaped HTML response, accepts only HTTPS `cdn.downloadgram.org` wrapper links, and classifies media from the wrapper token's embedded original URL. Several providers are retained but disabled by default in the workflow definitions.
- Fallback orchestration: `_instagram_service`, `_run_instagram_service`, `_run_instagram_services`, and `_instagram_failure_response`. DB configuration controls active order; success is the first handler that does not fail (and, when specified, returns non-empty `postData`).
- MySQL side effects: `update_download_history`, `update_frontend_success`, and `log_analytics`. Analytics dynamically adds provider-specific success/failure columns; provider names must therefore remain safe/stable.
- AI handlers: `_get_gemini` caches a Gemini client and retrieves its key from DB then environment; `_vision_gemini` uploads a temporary file and attempts remote cleanup; `_query_groq` makes OpenAI-compatible Groq calls. Upload handlers read the whole file into memory, use an untrusted temporary file name, and must clean it on every failure path.
- Tor and browser utilities: `get_tor_session`, `change_tor_ip`, `reset_instagram_identity`, and `setup_driver`. Tor state (`last_ip_change_time`) and the unbounded page-metadata cache are module globals. Selenium requires Chromium/Chrome and a matching driver; Docker uses `/usr/bin/chromium` and `/usr/bin/chromedriver`.

### Background operation and deployment

- `api/apify_key_rotator.py` obtains a MySQL advisory lock, decrypts/migrates tokens, polls Apify usage, updates `apify_keys`, disables a near-exhausted active key, selects the lowest-usage eligible key, and optionally sends SMTP alerts. It writes action records to `apify_rotation_logs`.
- `api/scheduler.py` runs that rotator in a subprocess at startup and every `ROTATOR_INTERVAL_MIN` minutes, with a 10-minute subprocess timeout, filtering selected warnings and appending to the configured log file.
- `Dockerfile` uses Python 3.9 slim and installs Chromium, Tor, curl, and required system libraries. `scripts/docker-entrypoint.sh` rewrites Tor configuration, waits for a Tor exit, starts the API and scheduler, then exits when either process exits. `scripts/deploy-refactor.sh` builds/deploys an image, migrates secrets, removes selected plaintext secrets from the environment file, and rolls back to the old image only when startup fails.
- `docs/tor_setup.txt` is an older manual Tor guide. Verify it against the Docker cookie-auth Tor configuration before changing or relying on it.

## 3. Runtime Flow

### Download media

`POST /download_media` receives multipart `instagramURL` and non-empty `deviceId` -> endpoint delegates to `instagram_service.download_media` -> `normalize_instagram_url` resolves supported `/share/` redirects, tries to decode `/s/` highlight links, removes query/fragment/trailing slash, and validates regexes -> non-Threads URLs run `check_instagram_privacy` through Instagram oEmbed (it fails open on request errors) -> profile URLs use a legacy `{code: 200, data: profile_url}` sentinel, all other valid URLs remain strings -> service definitions are merged with `download_media_service_config` -> each enabled service is attempted in configured order until one succeeds -> optional metadata enrichment scrapes Instagram page OpenGraph tags -> a success updates `insta_download_history` and `insta_analytics` -> response is `{code: 200, data: provider_result}`.

For ordinary post-like URLs, current in-code enabled services are RapidAPI, DownloadGram, GrabGram/SnapDownloader, SnapInsta, conditional oEmbed for confirmed photo posts, and Apify as final fallback. DownloadGram's default position is after RapidAPI and before SnapDownloader; the database still controls its enabled state and final order. For profiles, current enabled services are SnapDownloader, SnapInsta, conditional oEmbed, and Apify; DownloadGram is intentionally post-only until profile support is verified. Database configuration can disable/reorder these. The other listed services are disabled in code unless a future code change enables them. On total provider failure, the API intentionally returns HTTP-success-shaped `{code: 200, data: None, message: ...}` and records a failed history/analytics event. An invalid URL returns `{code: 400, message: ...}`; it is not an HTTP 400 response.

Provider implementations are sync and can block the FastAPI event loop for network/Selenium delays. Network-level failures usually become a fallback attempt, while privacy detection exits early with the generic unavailable response. No provider downloads media to local disk; it returns direct provider/CDN links. Provider result schemas vary, so the orchestrator only enforces non-empty media for services declared `require_post_data=True`.

### AI uploads

Caption/hashtag/transcription/hook endpoints accept multipart `UploadFile`, read its full content, write it to `NamedTemporaryFile(delete=False)`, call Gemini or Groq, parse a JSON fragment from model text with regex, and return `{code: 200, data: ...}`. Gemini remote files are deleted in `_vision_gemini`'s `finally`; local cleanup is inconsistent (`trendy_captions` only unlinks after success, while the other handlers clean up in their exception paths). Do not assume model output is valid JSON or that cleanup occurred unless a test proves it.

### Secret and scheduler flows

Startup calls `seed_env_settings`; it is best-effort so database unavailability does not prevent API startup. The `/crypto` routes use the same encryption implementation as repository migration. Separately, the entrypoint starts the scheduler, which executes `api.apify_key_rotator` as an isolated process to avoid sharing database/circuit state with the API.

### Health readiness

`GET /api/health` -> `health.readiness_payload` -> `health_service.check_database` -> a newly opened MySQL connection executes `SELECT 1` -> cursor/connection are closed -> the endpoint returns the payload with HTTP 200 when the database check is true or HTTP 503 when it is false. The endpoint is intended for load-balancer/readiness checks and does not alter database state.

## 4. Instagram URL Handling

`normalize_instagram_url` is the only request-entry URL validator. It accepts `http`/`https` hosts `instagram.com` and `www.instagram.com` for:

- standard `/p/<shortcode>` posts, including profile-prefixed variants;
- `/reel/<shortcode>` and legacy `/tv/<shortcode>`, including profile-prefixed variants;
- stories shaped `/stories/<username>/<numeric-id>` and the alternate profile-prefixed pattern;
- highlights shaped `/stories/highlights/<numeric-id>` and the alternate profile-prefixed pattern;
- bare Instagram profiles; and
- `threads.com`/`threads.net` `@<user>/post/<id>` URLs.

It strips query/fragment parameters, so tracking parameters and a trailing slash do not affect the normalized result. `/share/` requests follow redirects before regex validation; `/s/` links only become a supported highlight URL when their decoded payload has `highlight:<digits>`. It does not accept bare `instagram.com` without scheme, lookalike domains, `/reels/`, generic story-profile URLs, arbitrary Instagram routes, or a current separate IGTV product beyond legacy `/tv/` syntax. Regex validation protects the initial URL, but external provider redirects and provider-returned CDN links are trusted by the existing code; do not broaden accepted hosts or redirect behavior without an SSRF/security review.

Stories/highlights pass validation but are not routed to `fetch_story_or_highlight` by the current fallback definitions. Profile routes are supported as "return latest post through a provider" rather than as a profile metadata API; output depends on the provider. `_is_instagram_photo_post_url` may make an additional Instagram HTML request to distinguish a photo post from an ambiguous `/p/` video before giving oEmbed priority.

## 5. Important Invariants

- Preserve route names, multipart field names (`instagramURL`, `deviceId`, `video_file`, etc.), and established dictionary response shapes unless a requested API change explicitly permits a breaking change.
- Preserve `postData` media item keys and `GraphImage`/`GraphVideo` values. Clients consume direct links; no local downloader/file lifecycle exists in this API.
- Preserve the profile sentinel returned by `normalize_instagram_url` and handled by `download_media`, unless both callers and tests are deliberately updated.
- Do not turn the generic all-providers-failed response into an HTTP error without coordinating a client-contract change.
- Keep service identifiers stable: they drive DB configuration and dynamically created analytics columns. Understand the global `all` context behavior before changing profile/post ordering.
- Treat cookies, API keys, tokens, SMTP credentials, proxy data, and encryption values as secrets. Never log them; do not commit `.env` or modify existing secret configuration unnecessarily.
- Preserve retry/timeout/Tor semantics of a provider unless its callers and operational impact are understood. Provider sites and Instagram are unstable; deterministic parser tests should isolate that instability.
- The application relies on pre-existing MySQL tables `insta_download_history`, `insta_analytics`, `apify_keys`, and `apify_rotation_logs`; only some repository tables/columns are self-created. Schema changes need an explicit migration/compatibility plan.

## 6. Coding Conventions

- Target Python 3.9, matching `Dockerfile`; use standard-library typing (`Dict`, `List`, `Optional`) as the existing service does. Most code is PEP-8-ish but not formatter-enforced.
- Use FastAPI `Form`, `File`, and `UploadFile` at HTTP boundaries; route modules should delegate to services. Keep synchronous provider logic out of route modules.
- Repository functions open and close their own MySQL connection. Use parameterized SQL values; only construct identifiers after strict normalization/known constants.
- Existing logging uses `print` with useful provider/context labels. Keep diagnostic messages secret-free. Existing exception handling favors provider fallback and generic user output over propagating provider-specific errors.
- Add a focused helper/parser instead of duplicating provider parsing. Follow the normalized result schema and set `require_post_data=True` for handlers whose empty response must not be treated as success.
- Do not add dependencies casually: deployment depends on `requirements.txt`, Chromium, Tor, and shell tooling installed by Docker.

## 7. Change Protocol

Before editing, read the affected source and all callers, inspect the service definitions and their DB configuration implications, and search for tests/fixtures (none currently exist). Make the smallest coherent change. Update this document whenever architecture, routes, configuration, supported URLs, provider order, data shape, or testing requirements change.

For meaningful changes, add deterministic tests first where practical. Test URL normalization/classification and provider parsers with mocked HTTP/Selenium payloads; do not make CI depend on a live Instagram post. Run the targeted tests, then the full suite when one exists, and record every command actually executed. For changes affecting URL handling, providers, metadata, network behavior, or output formatting, exercise post/image, carousel, reel, story, highlight, profile, legacy `/tv/`, query variants, invalid/lookalike URLs, and restricted/unavailable behavior as applicable. Mark unexecuted live cases explicitly rather than claiming support.

## 8. Mandatory Automated QA

The repository currently provides no configured formatter, linter, type checker, or CI workflow. Deterministic DownloadGram coverage is in `tests/test_downloadgram.py` and runs with `python3 -m unittest tests.test_downloadgram`. At a minimum after Python edits, run that targeted test when relevant, `python3 -m unittest discover -s tests` when the suite is available, and a read-only AST parse when bytecode-cache permissions prevent `compileall`. Before changing Docker/deployment scripts, validate shell syntax and build/run behavior where environment access permits. Live Instagram, DownloadGram, provider, Tor, MySQL, Apify, Gemini, and Groq checks require their real credentials/services and must be reported as environment-dependent smoke tests, not deterministic regression proof.
