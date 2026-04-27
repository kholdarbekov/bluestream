import React from 'react';
import { render, screen } from '@testing-library/react';
import EmptyState from '../../components/common/EmptyState';

describe('EmptyState', () => {
  it('renders a description', () => {
    render(<EmptyState description="No reports here" />);
    expect(screen.getByText('No reports here')).toBeInTheDocument();
  });

  it('renders a CTA inside the empty container', () => {
    render(
      <EmptyState
        description="No reports"
        cta={<button type="button">Create</button>}
      />,
    );
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument();
  });

  it('falls back to children when description is not set', () => {
    render(<EmptyState>Nothing here</EmptyState>);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
  });
});
