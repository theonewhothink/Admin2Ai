/**
 * "The server said 401" as a tiny event, so every client in this JS runtime
 * (screens, the upload queue) can report it and the auth store, which lives
 * in the UI, can send the owner back to sign in. Pure: no native imports.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

export function onUnauthorized(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitUnauthorized(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // One listener failing must not stop the others.
    }
  }
}
