import { Spin, Alert, Button } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import EmptyState from './EmptyState';
import { extractApiErrorMessage } from '../../utils/apiError';

/**
 * Branches over loading / error / empty / ready with consistent UX.
 *
 * Lets callers express the four async UI states declaratively instead of
 * threading conditionals through their JSX. Matches the React Query
 * shape so a typical call is `<DataView loading={q.isLoading} error={q.error} ... />`.
 *
 * Resolution order: loading → error → empty → ready. The first matching
 * branch wins; pass falsy values for the ones that don't apply.
 */
const DataView = ({
  loading = false,
  error = null,
  isEmpty = false,
  onRetry,
  loadingFallback,
  errorFallback,
  emptyFallback,
  emptyDescription,
  emptyCta = null,
  retryLabel = 'Retry',
  minHeight = 160,
  children,
}) => {
  if (loading) {
    if (loadingFallback !== undefined) return <>{loadingFallback}</>;
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight,
          padding: 24,
        }}
      >
        <Spin />
      </div>
    );
  }

  if (error) {
    if (errorFallback !== undefined) return <>{errorFallback}</>;
    const description = extractApiErrorMessage(error, 'Something went wrong.');
    return (
      <Alert
        type="error"
        showIcon
        message={description}
        action={
          onRetry ? (
            <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
              {retryLabel}
            </Button>
          ) : null
        }
      />
    );
  }

  if (isEmpty) {
    if (emptyFallback !== undefined) return <>{emptyFallback}</>;
    return <EmptyState description={emptyDescription} cta={emptyCta} />;
  }

  return typeof children === 'function' ? children() : children;
};

export default DataView;
