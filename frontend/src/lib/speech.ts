// Browser speech for the MVP: SpeechRecognition (input) and speechSynthesis (output).
// The real-time avatar pipeline (ASR + TTS + lip-sync) replaces this behind the same hooks.
import { useCallback, useEffect, useRef, useState } from "react";

type Rec = any;

function recognitionCtor(): (new () => Rec) | null {
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export const speechInputSupported = () => recognitionCtor() !== null;
export const speechOutputSupported = () => typeof window !== "undefined" && "speechSynthesis" in window;

export function useSpeechInput(lang: string, onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const recRef = useRef<Rec | null>(null);
  const onFinalRef = useRef(onFinal);
  onFinalRef.current = onFinal;

  const stop = useCallback(() => {
    recRef.current?.stop();
  }, []);

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor || listening) return;
    window.speechSynthesis?.cancel();
    const rec = new Ctor();
    rec.lang = lang;
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e: any) => {
      let fin = "";
      let tmp = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) fin += t;
        else tmp += t;
      }
      setInterim(tmp);
      if (fin.trim()) {
        setInterim("");
        onFinalRef.current(fin.trim());
      }
    };
    rec.onend = () => {
      setListening(false);
      setInterim("");
    };
    rec.onerror = () => setListening(false);
    recRef.current = rec;
    rec.start();
    setListening(true);
  }, [lang, listening]);

  useEffect(() => () => recRef.current?.abort?.(), []);
  return { listening, interim, start, stop };
}

export function useSpeechOutput(lang: string) {
  const [speaking, setSpeaking] = useState(false);
  const speak = useCallback(
    (text: string) => {
      if (!speechOutputSupported()) return;
      const u = new SpeechSynthesisUtterance(text.replace(/•/g, "").replace(/\s+/g, " "));
      u.lang = lang;
      u.rate = 1;
      const voice = window.speechSynthesis.getVoices().find((v) => v.lang === lang) ||
        window.speechSynthesis.getVoices().find((v) => v.lang.startsWith("en"));
      if (voice) u.voice = voice;
      u.onstart = () => setSpeaking(true);
      u.onend = () => setSpeaking(false);
      u.onerror = () => setSpeaking(false);
      window.speechSynthesis.speak(u);
    },
    [lang],
  );
  const cancel = useCallback(() => {
    window.speechSynthesis?.cancel();
    setSpeaking(false);
  }, []);
  useEffect(() => () => window.speechSynthesis?.cancel(), []);
  return { speaking, speak, cancel };
}
