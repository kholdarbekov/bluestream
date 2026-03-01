export const extractApiErrorMessages = (error, fallbackMessage = 'An error occurred') => {
  const payload = error?.response?.data;

  if (Array.isArray(payload?.errors) && payload.errors.length > 0) {
    return payload.errors.filter(Boolean).map(String);
  }

  if (
    Array.isArray(payload?.details?.validation_errors) &&
    payload.details.validation_errors.length > 0
  ) {
    return payload.details.validation_errors.filter(Boolean).map(String);
  }

  if (typeof payload?.message === 'string' && payload.message.trim()) {
    return [payload.message.trim()];
  }

  if (typeof error?.message === 'string' && error.message.trim()) {
    return [error.message.trim()];
  }

  return [fallbackMessage];
};

export const extractApiErrorMessage = (error, fallbackMessage = 'An error occurred') =>
  extractApiErrorMessages(error, fallbackMessage)[0];
