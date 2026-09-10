/** Group decimal strings without rounding, coercion, or losing significant digits. */
export function formatDecimal(value: string | null | undefined): string {
  if (value === null || value === undefined) return '미확인';
  const match = /^([+-]?)(\d+)(\.\d+)?$/.exec(value);
  if (!match) return value;
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${match[3] ?? ''}`;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '시각 미확인';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '시각 미확인';
  return `${new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date)} KST`;
}

export function shortId(value: string): string {
  return value.length > 20 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

export function safeSourceUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password) return undefined;
    return url.href;
  } catch {
    return undefined;
  }
}
