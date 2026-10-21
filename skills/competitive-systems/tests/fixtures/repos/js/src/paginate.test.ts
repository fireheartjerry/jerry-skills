import { describe, expect, it } from 'vitest';
import { paginate } from '@/index';

describe('paginate', () => {
  it('returns the second page', () => {
    expect(paginate([1, 2, 3, 4, 5], 1)).toEqual([3, 4]);
  });
});
