import { Empty } from 'antd';

/**
 * Standard empty-state primitive.
 *
 * Wraps Ant Design's <Empty> with a consistent layout and an optional CTA
 * slot so pages stop reinventing their own "nothing to show" markup.
 * Named `EmptyState` to avoid shadowing antd's `Empty` at call sites.
 */
const EmptyState = ({
  description,
  image = Empty.PRESENTED_IMAGE_SIMPLE,
  cta = null,
  children,
  style,
  ...rest
}) => {
  return (
    <Empty
      image={image}
      description={description ?? children ?? null}
      style={{ padding: '24px 0', ...style }}
      {...rest}
    >
      {cta}
    </Empty>
  );
};

export default EmptyState;
