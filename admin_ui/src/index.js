import React, { useState, useEffect } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from 'react-query';
import { ConfigProvider } from 'antd';
import { Toaster } from 'react-hot-toast';
import i18n from './i18n'; // Initialize i18n
import './index.css';
import App from './App';

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