# Admin UI — Security Exceptions

This file tracks any `npm audit` findings that are knowingly accepted but **not yet fixed**. The `prebuild` script (`npm audit --audit-level=high --omit=dev`) blocks production builds on **high or critical** advisories in runtime dependencies. **Moderate** and **low** findings do not block the build, but anything that lingers should be listed here with an owner and a target date so the exception is visible and time-bounded.

If `npm audit fix` resolves a finding, fix it instead of adding it here.

## Rules

- **Critical / High**: never accepted. Either upgrade or remove the dependency. The build will fail until it is gone.
- **Moderate / Low**: accepted only with an explicit entry below. Each entry must include:
  - The advisory ID and a one-line summary.
  - **Why** the exception is acceptable today (data-flow analysis, no untrusted input, etc.).
  - **Target date** for resolution.
  - **Owner** — the person responsible for closing it out.
- Re-evaluate every entry on each release. Empty out entries that are resolved.

## Active exceptions

### GHSA-q89c-q3h5-w34g — `i18next-http-backend < 3.0.5`

- **Severity:** Moderate
- **Issue:** Path traversal / URL injection via unsanitised `lng`/`ns` parameters.
- **Why accepted:** The admin UI passes only its own internal namespace and language identifiers (`['common', 'navigation', 'dashboard', ...]` and `['uz', 'en', 'ru']`) to the loader — see [admin_ui/src/i18n.js](src/i18n.js). No user-controlled input reaches `lng` or `ns`, so the path-traversal vector is not reachable in our usage.
- **Resolution:** Bump to `i18next-http-backend@^3.0.5` (already publishes a fix). `npm audit fix` will pick this up.
- **Target:** Next dependency-bump pass.
- **Owner:** Admin UI maintainer.

### GHSA-w5hq-g745-h8pq — `uuid < 14.0.0` (transitive via `exceljs`)

- **Severity:** Moderate
- **Issue:** Missing buffer bounds check in `uuid` v3/v5/v6 when a `buf` argument is supplied.
- **Why accepted:** `uuid` is pulled in only as a transitive of `exceljs`, which the admin UI uses solely to **write** xlsx exports of admin-controlled data (see `src/utils/exportUtils.js`). No code path in the admin UI calls `uuid` with a caller-supplied `buf`, so the unsafe code path is never reached.
- **Resolution:** Wait for `exceljs` to release a version that depends on `uuid@^14`. Tracking upstream; do not downgrade `exceljs` (that would be a breaking change with no security benefit, since the unreachable advisory remains).
- **Target:** Re-evaluate when `exceljs@>=4.5` is published, or 2026-Q3 — whichever comes first.
- **Owner:** Admin UI maintainer.

## How to re-run the gate locally

```bash
cd admin_ui
npm audit --audit-level=high --omit=dev
```

Exit code `0` means the build will pass; non-zero means a high/critical vulnerability has appeared and must be fixed (or, in rare cases where it genuinely cannot be fixed, this file must be updated to lower the gate **after** the team agrees).
