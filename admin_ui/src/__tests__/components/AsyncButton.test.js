import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AsyncButton from '../../components/common/AsyncButton';

describe('AsyncButton', () => {
  it('renders children and forwards extra props', () => {
    render(<AsyncButton type="primary">Save</AsyncButton>);
    const btn = screen.getByRole('button', { name: 'Save' });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveClass('ant-btn-primary');
  });

  it('disables itself while a returned promise is pending and re-enables after resolve', async () => {
    let resolvePromise;
    const onClick = vi.fn(
      () => new Promise((resolve) => {
        resolvePromise = resolve;
      }),
    );
    const user = userEvent.setup();
    render(<AsyncButton onClick={onClick}>Submit</AsyncButton>);

    const btn = screen.getByRole('button', { name: /Submit/ });
    await user.click(btn);

    await waitFor(() => expect(btn).toBeDisabled());
    expect(onClick).toHaveBeenCalledTimes(1);

    resolvePromise();
    await waitFor(() => expect(btn).not.toBeDisabled());
  });

  it('re-enables itself after the promise rejects', async () => {
    let rejectPromise;
    const onClick = vi.fn(
      () => new Promise((_, reject) => {
        rejectPromise = reject;
      }),
    );
    const user = userEvent.setup();
    render(<AsyncButton onClick={onClick}>Go</AsyncButton>);

    const btn = screen.getByRole('button', { name: /Go/ });
    await user.click(btn);
    await waitFor(() => expect(btn).toBeDisabled());

    rejectPromise(new Error('boom'));
    await waitFor(() => expect(btn).not.toBeDisabled());
  });

  it('does not toggle pending state for synchronous handlers', async () => {
    const onClick = vi.fn(() => undefined);
    const user = userEvent.setup();
    render(<AsyncButton onClick={onClick}>Click</AsyncButton>);

    const btn = screen.getByRole('button', { name: /Click/ });
    await user.click(btn);
    expect(btn).not.toBeDisabled();
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('honours an explicit loading prop overriding internal state', () => {
    render(
      <AsyncButton loading onClick={() => Promise.resolve()}>
        Saving
      </AsyncButton>,
    );
    const btn = screen.getByRole('button');
    expect(btn).toBeDisabled();
  });
});
