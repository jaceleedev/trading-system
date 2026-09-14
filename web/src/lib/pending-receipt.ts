/** Recovery hints only. Explicit retries keep the original body; the server validates it. */
export type PendingScope = 'investigations' | 'capital' | 'paper' | 'outcomes';
type ReceiptStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const scopes: PendingScope[] = ['investigations', 'capital', 'paper', 'outcomes'];
const maxBytes = 2 * 1024 * 1024;

export class PendingReceiptError extends Error {
  constructor() {
    super(
      '원래 실행 요청을 브라우저에 보관하거나 읽지 못했습니다. 중복 실행을 막기 위해 새 요청을 멈췄습니다. 저장 상태를 확인해 주세요.',
    );
    this.name = 'PendingReceiptError';
  }
}
export function pendingReceiptKey(workspace: string, scope: PendingScope) {
  if (!workspace || workspace.length > 256 || !scopes.includes(scope))
    throw new PendingReceiptError();
  return `trading-pending-v1:${encodeURIComponent(workspace)}:${scope}`;
}
function storageOrDefault(storage?: ReceiptStorage): ReceiptStorage {
  return storage ?? window.localStorage;
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function readPendingReceipt<T>(
  workspace: string,
  scope: PendingScope,
  storage?: ReceiptStorage,
): T | null {
  try {
    const raw = storageOrDefault(storage).getItem(pendingReceiptKey(workspace, scope));
    if (raw === null) return null;
    if (raw.length > maxBytes) throw new PendingReceiptError();
    const value = JSON.parse(raw);
    if (
      !object(value) ||
      value.version !== 1 ||
      value.workspace !== workspace ||
      value.scope !== scope ||
      !object(value.pending)
    )
      throw new PendingReceiptError();
    return value.pending as T;
  } catch {
    throw new PendingReceiptError();
  }
}
/** Call synchronously before POST, not from a delayed reactive effect. Never automatically submit. */
export function writePendingReceipt(
  workspace: string,
  scope: PendingScope,
  pending: unknown | null,
  storage?: ReceiptStorage,
): void {
  try {
    const target = storageOrDefault(storage);
    const key = pendingReceiptKey(workspace, scope);
    if (pending === null) {
      target.removeItem(key);
      if (target.getItem(key) !== null) throw new PendingReceiptError();
      return;
    }
    if (!object(pending)) throw new PendingReceiptError();
    const raw = JSON.stringify({ version: 1, workspace, scope, pending });
    if (raw.length > maxBytes) throw new PendingReceiptError();
    target.setItem(key, raw);
    if (target.getItem(key) !== raw) throw new PendingReceiptError();
  } catch {
    throw new PendingReceiptError();
  }
}
