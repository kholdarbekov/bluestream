import i18next from 'i18next';
import { describe, it, expect } from 'vitest';
import { createParseMissingKeyHandler } from '../missingTranslation';

// Drives a real i18next instance rather than calling the handler directly: the
// bug being pinned here is about i18next's parseMissingKeyHandler contract (it
// passes the resolved default as the SECOND argument and REPLACES the result
// with whatever the handler returns), which a direct call would not exercise.
const makeI18n = ({ isDevelopment = false } = {}) => {
  const instance = i18next.createInstance();
  instance.init({
    lng: 'en',
    fallbackLng: false,
    initImmediate: false,
    // Flat dotted keys, exactly as the translations API serves them.
    resources: { en: { translation: { 'ui.common.save': 'Save' } } },
    interpolation: { escapeValue: false },
    parseMissingKeyHandler: createParseMissingKeyHandler(isDevelopment),
  });
  return instance;
};

describe('createParseMissingKeyHandler', () => {
  it('renders the fallback the call site passed when the key is missing', () => {
    const { t } = makeI18n();

    expect(t('ui.common.cancel', 'Cancel')).toBe('Cancel');
  });

  it('renders the database value when the key exists', () => {
    const { t } = makeI18n();

    expect(t('ui.common.save', 'Save')).toBe('Save');
  });

  it('interpolates the fallback rather than returning it raw', () => {
    const { t } = makeI18n();

    expect(t('ui.users.greeting', 'Hello {{name}}', { name: 'Umar' })).toBe('Hello Umar');
  });

  it('renders the bare key in production when no fallback was passed', () => {
    const { t } = makeI18n();

    expect(t('ui.common.cancel')).toBe('ui.common.cancel');
  });

  it('flags a fallback-less missing key in development', () => {
    const { t } = makeI18n({ isDevelopment: true });

    expect(t('ui.common.cancel')).toBe('⚠️ ui.common.cancel');
  });

  it('still honours the fallback in development', () => {
    const { t } = makeI18n({ isDevelopment: true });

    expect(t('ui.common.cancel', 'Cancel')).toBe('Cancel');
  });
});
