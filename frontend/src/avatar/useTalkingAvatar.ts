import { useCallback, useEffect, useRef, useState } from "react";
import { AvatarSession, type AvatarState } from "./AvatarSession";

/**
 * The talking avatar for one consultation screen. The canvas goes into the Mirror; `speak` returns false when the
 * avatar can't take the line (not set up, or unavailable), so the caller uses the browser's voice instead.
 */
export function useTalkingAvatar(enabled: boolean) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sessionRef = useRef<AvatarSession | null>(null);
  const [state, setState] = useState<AvatarState>(enabled ? "connecting" : "off");
  const [detail, setDetail] = useState<string | undefined>();
  const [speaking, setSpeaking] = useState(false);
  const fallbackRef = useRef<((lines: string[]) => void) | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!enabled || !canvas) {
      setState("off");
      return;
    }
    const session = new AvatarSession(canvas, {
      onState: (s, d) => {
        setState(s);
        setDetail(d);
        // Lines the avatar will now never say are spoken by the browser instead.
        if (s === "unavailable" || s === "off") {
          const left = session.takeQueue();
          if (left.length) fallbackRef.current?.(left);
        }
      },
      onSpeaking: setSpeaking,
    });
    sessionRef.current = session;
    session.start();
    return () => {
      session.destroy();
      sessionRef.current = null;
    };
  }, [enabled]);

  const speak = useCallback((text: string): boolean => {
    const s = sessionRef.current;
    if (!s || !s.usable) return false;
    s.speak(text);
    return true;
  }, []);

  const stop = useCallback(() => sessionRef.current?.stop(), []);
  const retry = useCallback(() => sessionRef.current?.retry(), []);
  const onFallback = useCallback((fn: (lines: string[]) => void) => {
    fallbackRef.current = fn;
  }, []);

  return { canvasRef, state, detail, speaking, speak, stop, retry, onFallback, active: state === "ready" || state === "connecting" || state === "resting" };
}
