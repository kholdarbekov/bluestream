import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import HttpBackend from 'i18next-http-backend';
import LanguageDetector from 'i18next-browser-languagedetector';

// Get API base URL from environment or use proxy
const API_BASE_URL = process.env.REACT_APP_API_URL || 'https://aqua-element.uz/api/v1';

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

    // Backend options - Load from database via API
    backend: {
      loadPath: `${API_BASE_URL}/translations/{{lng}}/{{ns}}`,
      crossDomain: false,
      withCredentials: true, // Include cookies for authentication
    },

    // Namespace configuration - each namespace maps to a ui_* category in the database
    ns: ['common', 'navigation', 'dashboard', 'orders', 'products', 'users', 'settings', 'profile', 'analytics', 'blog', 'delivery', 'loyalty', 'login', 'staff'],
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
    // Format missing keys with visual indicator
    parseMissingKeyHandler: (key) => {
      if (process.env.NODE_ENV === 'development') {
        return `⚠️ ${key}`;
      }
      return key;
    },
  });

// Export reload function for manual sync
export const reloadTranslations = async () => {
  await i18n.reloadResources();
  return true;
};

export default i18n;
