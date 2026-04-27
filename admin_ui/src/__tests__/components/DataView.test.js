import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DataView from '../../components/common/DataView';

describe('DataView', () => {
  it('renders the loading branch when loading is true', () => {
    render(
      <DataView loading>
        <div>ready</div>
      </DataView>,
    );
    expect(screen.queryByText('ready')).not.toBeInTheDocument();
    expect(document.querySelector('.ant-spin')).toBeInTheDocument();
  });

  it('renders the error branch with a retry action when onRetry is provided', async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(
      <DataView error={{ message: 'Network down' }} onRetry={onRetry}>
        <div>ready</div>
      </DataView>,
    );

    expect(screen.getByText('Network down')).toBeInTheDocument();
    const retryBtn = screen.getByRole('button', { name: /retry/i });
    await user.click(retryBtn);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('ready')).not.toBeInTheDocument();
  });

  it('omits the retry button when onRetry is not provided', () => {
    render(
      <DataView error={new Error('Bad')}>
        <div>ready</div>
      </DataView>,
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders the empty branch with description and CTA', () => {
    render(
      <DataView
        isEmpty
        emptyDescription="No orders yet"
        emptyCta={<button type="button">Create one</button>}
      >
        <div>ready</div>
      </DataView>,
    );
    expect(screen.getByText('No orders yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create one' })).toBeInTheDocument();
    expect(screen.queryByText('ready')).not.toBeInTheDocument();
  });

  it('renders children when no other branch matches', () => {
    render(
      <DataView>
        <div>ready</div>
      </DataView>,
    );
    expect(screen.getByText('ready')).toBeInTheDocument();
  });

  it('supports a render-prop child', () => {
    render(
      <DataView>{() => <div>render-prop</div>}</DataView>,
    );
    expect(screen.getByText('render-prop')).toBeInTheDocument();
  });

  it('prefers loading over error when both are set', () => {
    render(
      <DataView loading error={new Error('ignored')}>
        ready
      </DataView>,
    );
    expect(document.querySelector('.ant-spin')).toBeInTheDocument();
    expect(screen.queryByText('ignored')).not.toBeInTheDocument();
  });
});
