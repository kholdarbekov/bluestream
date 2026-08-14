import { describe, it, expect } from 'vitest';
import { parseCoordinates } from '../parseCoordinates';

describe('parseCoordinates', () => {
  it('parses the comma-separated pair admins paste from Telegram', () => {
    expect(parseCoordinates('41.323396, 69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('parses a pair with no space after the comma', () => {
    expect(parseCoordinates('41.323396,69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('parses a space-separated pair', () => {
    expect(parseCoordinates('41.323396 69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('ignores surrounding whitespace', () => {
    expect(parseCoordinates('   41.323396 ,  69.193840   ')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('parses the "Lat: x, Lng: y" line this app prints under the map', () => {
    expect(parseCoordinates('Lat: 41.323396, Lng: 69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('parses the lowercase lat/long label variant', () => {
    expect(parseCoordinates('lat 41.323396 long 69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('parses the "lon" label variant', () => {
    expect(parseCoordinates('lat: -41.5, lon: -69.25')).toEqual({
      latitude: -41.5,
      longitude: -69.25,
    });
  });

  it('parses whole-number coordinates', () => {
    expect(parseCoordinates('41, 69')).toEqual({ latitude: 41, longitude: 69 });
  });

  it('parses negative coordinates in both hemispheres', () => {
    expect(parseCoordinates('-33.868820, -151.209290')).toEqual({
      latitude: -33.86882,
      longitude: -151.20929,
    });
  });

  it('parses a semicolon-separated pair', () => {
    expect(parseCoordinates('41.323396; 69.193840')).toEqual({
      latitude: 41.323396,
      longitude: 69.19384,
    });
  });

  it('rejects an address that happens to contain two numbers', () => {
    expect(parseCoordinates('Chilonzor 12, 34')).toBeNull();
  });

  it('rejects a plain street address', () => {
    expect(parseCoordinates('Amir Temur street 15')).toBeNull();
  });

  it('rejects a single number', () => {
    expect(parseCoordinates('41.323396')).toBeNull();
  });

  it('rejects three numbers', () => {
    expect(parseCoordinates('41.323396, 69.193840, 12')).toBeNull();
  });

  it('rejects a latitude beyond the poles', () => {
    expect(parseCoordinates('91.5, 69.193840')).toBeNull();
  });

  it('rejects a latitude below the south pole', () => {
    expect(parseCoordinates('-90.0001, 69.193840')).toBeNull();
  });

  it('rejects a longitude beyond the antimeridian', () => {
    expect(parseCoordinates('41.323396, 180.5')).toBeNull();
  });

  it('accepts the exact range boundaries', () => {
    expect(parseCoordinates('90, 180')).toEqual({ latitude: 90, longitude: 180 });
    expect(parseCoordinates('-90, -180')).toEqual({ latitude: -90, longitude: -180 });
  });

  it('rejects empty and blank input', () => {
    expect(parseCoordinates('')).toBeNull();
    expect(parseCoordinates('   ')).toBeNull();
  });

  it('rejects non-string input', () => {
    expect(parseCoordinates(null)).toBeNull();
    expect(parseCoordinates(undefined)).toBeNull();
    expect(parseCoordinates(41.32)).toBeNull();
  });

  it('rejects a malformed number', () => {
    expect(parseCoordinates('41..3, 69.1')).toBeNull();
  });
});
