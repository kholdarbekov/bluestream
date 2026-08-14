/**
 * Parses a pasted "latitude, longitude" pair into coordinates.
 *
 * Admins receive a geolocation from a customer over Telegram and paste the raw
 * pair into the address search box; this is what lets that pin the map instead
 * of running a doomed address geocode. It also accepts the
 * "Lat: 41.323396, Lng: 69.193840" line the map picker prints under the map,
 * because that is a real copy source inside the admin UI itself.
 *
 * Anything that is not unambiguously a coordinate pair returns null, so an
 * address like "Chilonzor 12, 34" still falls through to the address search.
 *
 * @param {string} input - Raw text from the search box.
 * @returns {{latitude: number, longitude: number} | null}
 */
const LABELS = /\b(latitude|longitude|long|lon|lng|lat)\b/gi;
const SEPARATORS = /[,;\s]+/;
// Flat alternation rather than an optional fraction group: a quantifier nested
// inside a quantified group trips eslint's detect-unsafe-regex.
const NUMBER = /^[-+]?(\d+|\d+\.\d+)$/;

export const parseCoordinates = (input) => {
  if (typeof input !== 'string') return null;

  const stripped = input.replace(LABELS, ' ').replace(/:/g, ' ').trim();
  if (!stripped) return null;

  // Exactly two tokens, both plain numbers: "Chilonzor 12, 34" splits into
  // three and "Chilonzor 12" fails the number test, so addresses fall through
  // to the address geocoder untouched.
  const tokens = stripped.split(SEPARATORS).filter(Boolean);
  if (tokens.length !== 2 || !tokens.every((token) => NUMBER.test(token))) return null;

  const latitude = Number(tokens[0]);
  const longitude = Number(tokens[1]);

  if (latitude < -90 || latitude > 90) return null;
  if (longitude < -180 || longitude > 180) return null;

  return { latitude, longitude };
};

export default parseCoordinates;
