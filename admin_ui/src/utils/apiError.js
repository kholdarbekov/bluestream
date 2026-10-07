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

// A refusal's `error_code`, from either envelope (§6.5): older routes answer it under `data`,
// `handle_api_exception` at the top of the body. The interceptor's "the request named this code"
// check and every caller that explains a refusal itself read it here, so the two cannot drift
// apart and leave a refusal both silenced and unexplained.
export const apiErrorCode = (error) =>
  error?.response?.data?.data?.error_code ?? error?.response?.data?.error_code;
