/**
 * Kept in step with backend/app/version.py and both manifests -- a test in
 * the backend suite fails if they drift. Inlined rather than fetched: the
 * shell renders before any request completes, and a version that appears a
 * second late is a version nobody sees.
 */
export const VERSION = "0.3.0";
