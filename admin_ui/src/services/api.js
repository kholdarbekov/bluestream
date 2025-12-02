import axios from 'axios';
import toast from 'react-hot-toast';

// Create axios instance with cookie support
console.log('API URL:', process.env.REACT_APP_API_URL);
const api = axios.create({
  baseURL: process.env.REACT_APP_API_URL || 'https://bluestream.uz/api/v1',
  timeout: 10000,
  withCredentials: true  // Enable sending cookies with requests
});

// CSRF token management
let csrfToken = null;

// Helper function to get cookie value by name
const getCookie = (name) => {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(';').shift();
  return null;
};

// Function to get JWT CSRF token from cookie
const getJWTCSRFToken = () => {
  // Flask-JWT-Extended stores CSRF token in cookies with name 'csrf_access_token'
  return getCookie('csrf_access_token');
};

// Function to fetch CSRF token
const fetchCSRFToken = async () => {
  try {
    const response = await axios.get(
      `${process.env.REACT_APP_API_URL || 'https://bluestream.uz/api/v1'}/csrf-token`,
      { withCredentials: true }
    );
    csrfToken = response.data.csrf_token;
    return csrfToken;
  } catch (error) {
    console.error('Failed to fetch CSRF token:', error);
    return null;
  }
};

// Initialize CSRF token on module load
fetchCSRFToken();

// Request interceptor to set headers
api.interceptors.request.use(
  async (config) => {
    // Add CSRF token for state-changing methods
    if (['post', 'put', 'patch', 'delete'].includes(config.method.toLowerCase())) {
      // First, try to get JWT CSRF token from cookie (for JWT-protected endpoints)
      let tokenToUse = getJWTCSRFToken();

      // If no JWT CSRF token, use the general CSRF token
      if (!tokenToUse) {
        if (!csrfToken) {
          await fetchCSRFToken();
        }
        tokenToUse = csrfToken;
      }

      // Add CSRF token to headers
      if (tokenToUse) {
        config.headers['X-CSRF-TOKEN'] = tokenToUse;
      }
    }

    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor to handle errors
api.interceptors.response.use(
  (response) => {
    // Update CSRF token if provided in response headers
    const newCsrfToken = response.headers['x-csrftoken'];
    if (newCsrfToken) {
      csrfToken = newCsrfToken;
    }

    return response;
  },
  async (error) => {
    const message = error.response?.data?.message || error.message || 'An error occurred';

    // Handle CSRF token errors
    if (error.response?.status === 400 &&
        (message.toLowerCase().includes('csrf') || error.response?.data?.error?.toLowerCase().includes('csrf'))) {
      console.log('CSRF token error, fetching new token...');
      await fetchCSRFToken();

      // Retry the request with new CSRF token
      if (csrfToken && error.config && !error.config.__isRetry) {
        error.config.__isRetry = true;
        error.config.headers['X-CSRF-TOKEN'] = csrfToken;
        return api.request(error.config);
      }

      toast.error('Security token expired. Please try again.');
      return Promise.reject(error);
    }

    // Handle JWT CSRF errors (422 status from Flask-JWT-Extended)
    if (error.response?.status === 422 &&
        (message.toLowerCase().includes('csrf') || error.response?.data?.msg?.toLowerCase().includes('csrf'))) {
      console.log('JWT CSRF token error, retrying with token from cookie...');

      // Retry the request with JWT CSRF token from cookie
      const jwtCsrfToken = getJWTCSRFToken();
      if (jwtCsrfToken && error.config && !error.config.__isRetry) {
        error.config.__isRetry = true;
        error.config.headers['X-CSRF-TOKEN'] = jwtCsrfToken;
        return api.request(error.config);
      }

      toast.error('Security token expired. Please try again.');
      return Promise.reject(error);
    }

    if (error.response?.status === 401) {
      // Clear any remaining localStorage data (migration cleanup)
      localStorage.removeItem('admin_token');
      localStorage.removeItem('admin_user');
      localStorage.removeItem('admin_permissions');
      window.location.href = '/login';
      toast.error('Session expired. Please login again.');
    } else if (error.response?.status === 403) {
      toast.error('Access denied. Insufficient permissions.');
    } else if (error.response?.status >= 500) {
      toast.error('Server error. Please try again later.');
    } else {
      toast.error(message);
    }

    return Promise.reject(error);
  }
);

// Export the API instance and CSRF token management
export { fetchCSRFToken };
export default api;
