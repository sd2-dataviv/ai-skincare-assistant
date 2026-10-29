import { useCallback, useEffect, useRef, useState } from "react";
import { getToken } from "../lib/api";
import { useSpeechOutput } from "../lib/speech";

/**
 * Voice-only mode, used whenever the talking avatar can't say a reply. The avatar service's speech engine says it in
 * the avatar's own voice (POST /api/avatar/speech, a WAV played through Web Audio so the orb can follow the level);
 * if that can't be used, the browser's built-in voice says it instead.
 */
const RETRY_ENGINE_AFTER_MS = 60_000;

export function useVoice(lang: string) {
  const { speak: browserSpeak, cancel: browserCancel, speaking: browserSpeaking } = useSpeechOutput(lang);
  const [engineSpeaking, setEngineSpeaking] = useState(false);
  const queue = useRef<string[]>([]);
  const busy = useRef(false);
  const gen = useRef(0);
  const ctxRef = useRef<AudioContext | null>(null);
  const analyser = useRef<AnalyserNode | null>(null);
  const source = useRef<AudioBufferSourceNode | null>(null);
  const samples = useRef<Uint8Array | null>(null);
  const engineOffUntil = useRef(0); // after a failure, the browser's voice is used for a while
  const engineMissing = useRef(false); // 404: not set up here at all

  const audio = useCallback((): AudioContext => {
    if (!ctxRef.current) {
      const ctx = new AudioContext();
      const a = ctx.createAnalyser();
      a.fftSize = 1024;
      a.connect(ctx.destination);
      ctxRef.current = ctx;
      analyser.current = a;
      samples.current = new Uint8Array(a.fftSize);
    }
    if (ctxRef.current.state === "suspended") ctxRef.current.resume().catch(() => undefined);
    return ctxRef.current;
  }, []);

  const fetchSpeech = useCallback(async (text: string): Promise<ArrayBuffer | null> => {
    if (engineMissing.current || Date.now() < engineOffUntil.current) return null;
    try {
      const token = getToken();
      const res = await fetch("/api/avatar/speech", {
        method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ text }),
      });
      if (res.status === 404) engineMissing.current = true;
      if (!res.ok) {
        if (res.status !== 404) engineOffUntil.current = Date.now() + RETRY_ENGINE_AFTER_MS;
        return null;
      }
      return await res.arrayBuffer();
    } catch {
      engineOffUntil.current = Date.now() + RETRY_ENGINE_AFTER_MS;
      return null;
    }
  }, []);

  const pump = useCallback(async () => {
    if (busy.current) return;
    const text = queue.current.shift();
    if (text === undefined) {
      setEngineSpeaking(false);
      return;
    }
    busy.current = true;
    const mine = gen.current;
    const wav = await fetchSpeech(text);
    if (mine !== gen.current) return; // cancelled while fetching
    if (!wav) {
      busy.current = false;
      browserSpeak(text); // the browser queues its own lines
      void pump();
      return;
    }
    try {
      const ctx = audio();
      const buf = await ctx.decodeAudioData(wav);
      if (mine !== gen.current) return;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(analyser.current!);
      source.current = src;
      src.onended = () => {
        if (source.current === src) source.current = null;
        if (mine !== gen.current) return;
        busy.current = false;
        void pump();
      };
      setEngineSpeaking(true);
      src.start();
    } catch {
      busy.current = false;
      browserSpeak(text);
      void pump();
    }
  }, [audio, browserSpeak, fetchSpeech]);

  const speak = useCallback((text: string) => {
    const t = text.trim();
    if (!t) return;
    queue.current.push(t);
    void pump();
  }, [pump]);

  const cancel = useCallback(() => {
    gen.current += 1;
    queue.current = [];
    busy.current = false;
    try {
      source.current?.stop();
    } catch {
      /* already stopped */
    }
    source.current = null;
    setEngineSpeaking(false);
    browserCancel();
  }, [browserCancel]);

  /** 0..1 for the orb: the real level of the avatar's voice, or a lively stand-in for the browser's voice. */
  const level = useCallback((): number => {
    if (source.current && analyser.current && samples.current) {
      analyser.current.getByteTimeDomainData(samples.current);
      let sum = 0;
      for (const v of samples.current) sum += (v - 128) * (v - 128);
      return Math.min(1, Math.sqrt(sum / samples.current.length) / 40);
    }
    if (browserSpeaking) {
      const t = performance.now() / 1000;
      return 0.35 + 0.3 * Math.abs(Math.sin(t * 7.3) * Math.sin(t * 2.9));
    }
    return 0;
  }, [browserSpeaking]);

  useEffect(() => () => {
    gen.current += 1;
    try {
      source.current?.stop();
    } catch {
      /* already stopped */
    }
    ctxRef.current?.close().catch(() => undefined);
  }, []);

  return { speak, cancel, level, speaking: engineSpeaking || browserSpeaking };
}
