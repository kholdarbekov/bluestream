import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import HttpBackend from 'i18next-http-backend';
import LanguageDetector from 'i18next-browser-languagedetector';
import { createParseMissingKeyHandler } from './utils/missingTranslation';

// Get API base URL from environment or use proxy
const API_BASE_URL = import.meta.env.VITE_API_URL || 'https://aqua-element.uz/api/v1';

i18n
  // Load translations using http backend
  .use(HttpBackend)
  // Detect user language
  .use(LanguageDetector)
  // Pass the i18n instance to react-i18next
  .use(initReactI18next)
  // Initialize i18next
  .init({
    fallbackLng: 'uz', // Default language is Uzbek
    supportedLngs: ['uz', 'en', 'ru'], // Supported languages

    // Language detection options
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage'],
      lookupLocalStorage: 'i18nextLng',
    },

    // Backend options - Load from database via API.
    //
    // `allowMultiLoading: true` makes i18next-http-backend join all pending
    // namespaces with `+` into a single HTTP request, e.g.
    //   GET /api/v1/translations/uz/common+navigation+dashboard+...
    // Backend (business_app/api/translations.py) returns the expected
    // `{lng: {ns: {...}}}` envelope. Without this, i18next would fire 14
    // parallel requests on every cold load and bust the nginx api_limit
    // burst window — exactly the bug this change is fixing.
    backend: {
      loadPath: `${API_BASE_URL}/translations/{{lng}}/{{ns}}`,
      allowMultiLoading: true,
      crossDomain: false,
      withCredentials: true, // Include cookies for authentication
    },

    // Namespace configuration - each namespace maps to a ui_* category in the database
    ns: ['common', 'navigation', 'dashboard', 'orders', 'products', 'users', 'settings', 'profile', 'analytics', 'blog', 'delivery', 'loyalty', 'login', 'staff', 'subscriptions', 'bottle_tracking', 'time_slots', 'product_categories', 'translations_page', 'tryouts', 'notifications'],
    defaultNS: 'common',
    // Most admin UI keys are still stored as full ui.* keys in the shared namespace.
    // Feature namespaces should fall back to common while scoped ui_* categories are phased in.
    fallbackNS: ['common'],

    // Interpolation
    interpolation: {
      escapeValue: false, // React already escapes values
    },

    // React specific options
    react: {
      useSuspense: true, // Enable suspense
    },

    // Development mode
    debug: process.env.NODE_ENV === 'development',

    // Missing key handling - Show key name with highlighting in development
    saveMissing: process.env.NODE_ENV === 'development',
    missingKeyHandler: (lngs, ns, key, fallbackValue) => {
      if (process.env.NODE_ENV === 'development') {
        console.warn(`Missing translation: [${ns}] ${key} for languages: ${lngs.join(', ')}`);
      }
    },
    // Falls back to the English default the call site passed, and only marks
    // the key when there is nothing to fall back to. See missingTranslation.js
    // for why accepting i18next's second argument is load-bearing.
    parseMissingKeyHandler: createParseMissingKeyHandler(
      process.env.NODE_ENV === 'development'
    ),
  });

// Export reload function for manual sync
export const reloadTranslations = async () => {
  await i18n.reloadResources();
  return true;
};

export default i18n;
