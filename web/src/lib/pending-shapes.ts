export function pendingObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
export function pendingKey(value: unknown) {
  return (
    pendingObject(value) &&
    typeof value.request_key === 'string' &&
    value.request_key.length > 0 &&
    value.request_key.length <= 200
  );
}
export function pendingNullable(value: unknown, check: (value: unknown) => boolean) {
  return value === null || check(value);
}
