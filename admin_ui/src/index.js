import React, { useState, useEffect } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import { Toaster } from 'react-hot-toast';
import * as Sentry from '@sentry/react';
import i18n from './i18n'; // Initialize i18n
import './index.css';
import App from './App';

const sentryDsn = import.meta.env.VITE_SENTRY_DSN;
if (sentryDsn) {
  const SENSITIVE_RE = /password|token|secret|authorization|credit_card|card_number|cvv|pin|ssn|passport|api_key|access_token|refresh_token/i;
  const scrub = (value) => {
    if (Array.isArray(value)) return value.map(scrub);
    if (value && typeof value === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(value)) {
        // eslint-disable-next-line security/detect-object-injection
        out[k] = SENSITIVE_RE.test(k) ? '[REDACTED]' : scrub(v);
      }
      return out;
    }
    return value;
  };

  Sentry.init({
    dsn: sentryDsn,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE,
    release: import.meta.env.VITE_SENTRY_RELEASE,
    tracesSampleRate: 0.05,
    sendDefaultPii: false,
    beforeSend(event) {
      if (event.request?.data) event.request.data = scrub(event.request.data);
      if (event.extra) event.extra = scrub(event.extra);
      if (event.contexts) event.contexts = scrub(event.contexts);
      return event;
    },
  });
}

// Import Ant Design locales
import uzUZ from 'antd/locale/uz_UZ';
import enUS from 'antd/locale/en_US';
import ruRU from 'antd/locale/ru_RU';

// Create a client
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false
    }
  }
});

// Ant Design theme configuration
const theme = {
  token: {
    colorPrimary: '#1890ff',
    colorSuccess: '#52c41a',
    colorWarning: '#faad14',
    colorError: '#ff4d4f',
    borderRadius: 6
  }
};

// Locale mapping
const localeMap = {
  uz: uzUZ,
  en: enUS,
  ru: ruRU
};

// Root component with locale support
const Root = () => {
  const [locale, setLocale] = useState(localeMap[i18n.language] || uzUZ);

  useEffect(() => {
    // Update locale when language changes
    const handleLanguageChange = (lng) => {
      // eslint-disable-next-line security/detect-object-injection
      setLocale(localeMap[lng] || uzUZ);
    };

    // Listen for language changes
    i18n.on('languageChanged', handleLanguageChange);

    // Set initial locale
    setLocale(localeMap[i18n.language] || uzUZ);

    // Cleanup
    return () => {
      i18n.off('languageChanged', handleLanguageChange);
    };
  }, []);

  return (
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <ConfigProvider theme={theme} locale={locale}>
          <BrowserRouter>
            <App />
            <Toaster
              position="top-right"
              toastOptions={{
                duration: 4000,
                style: {
                  background: '#363636',
                  color: '#fff'
                }
              }}
            />
          </BrowserRouter>
        </ConfigProvider>
      </QueryClientProvider>
    </React.StrictMode>
  );
};

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<Root />);
