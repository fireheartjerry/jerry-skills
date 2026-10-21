export function paginate<T>(items: T[], page: number, size = 2): T[] {
  return items.slice(page * size, (page + 1) * size);
}
