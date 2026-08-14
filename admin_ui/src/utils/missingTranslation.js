/**
 * Builds i18next's `parseMissingKeyHandler`.
 *
 * i18next calls this handler for every key it could not resolve, and REPLACES
 * the result with whatever the handler returns — including when the call site
 * supplied a fallback, which arrives as the second argument already
 * interpolated. A handler that only accepts `(key)` therefore throws the
 * fallback away, so `t('ui.common.cancel', 'Cancel')` rendered the literal
 * string "ui.common.cancel" in production for every key absent from the
 * database. Honouring `defaultValue` is what keeps the admin UI readable when a
 * translation row is missing or miscategorised.
 *
 * @param {boolean} isDevelopment - Mark fallback-less missing keys for devs.
 * @returns {(key: string, defaultValue?: string) => string}
 */
export const createParseMissingKeyHandler = (isDevelopment) => (key, defaultValue) => {
  if (defaultValue !== undefined) {
    return defaultValue;
  }
  // No fallback to fall back to: flag it in development, stay quiet in
  // production. `missingKeyHandler` still logs these to the console in dev.
  return isDevelopment ? `⚠️ ${key}` : key;
};

export default createParseMissingKeyHandler;
