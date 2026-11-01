import { useCallback, useRef, useState } from "react";

/** Apply the guard synchronously, including two handlers before React renders. */
export function useMachine<S, E>(initial: S, transition: (state: S, event: E) => S) {
  const [state, setState] = useState(initial);
  const current = useRef(initial);
  const send = useCallback((event: E) => {
    const next = transition(current.current, event);
    current.current = next;
    setState(next);
    return next;
  }, [transition]);
  const read = useCallback(() => current.current, []);
  return { state, send, read };
}
