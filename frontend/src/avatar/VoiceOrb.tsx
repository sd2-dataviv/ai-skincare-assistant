import { useEffect, useRef } from "react";

/**
 * Voice-only mode: a Siri-style orb in place of the avatar. Soft coloured lobes swirl inside a glowing sphere and
 * swell with the voice: from the real audio level when the avatar's voice is playing, a lively stand-in when the
 * browser's own voice speaks. Listening adds a pulsing ring; thinking turns the lobes slowly.
 */
export type OrbState = "idle" | "listening" | "thinking" | "speaking";

interface Props {
  state: OrbState;
  /** 0..1, read every frame while speaking. */
  level?: () => number;
}

// Luminous lobes, blended additively inside a dark glass sphere: teal (the app's eucalyptus, brightened), rose,
// warm peach and violet.
const LOBES = [
  { color: [56, 214, 196], k: [2, 3, 5], speed: 0.55, phase: 0 },
  { color: [255, 112, 166], k: [3, 2, 4], speed: -0.7, phase: 2.1 },
  { color: [255, 178, 110], k: [2, 4, 3], speed: 0.85, phase: 4.2 },
  { color: [138, 125, 255], k: [3, 5, 2], speed: -0.45, phase: 1.3 },
];

export function VoiceOrb({ state, level }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const levelRef = useRef(level);
  levelRef.current = level;

  useEffect(() => {
    const canvas = ref.current;
    const cx = canvas?.getContext("2d");
    if (!canvas || !cx) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    let raf = 0;
    let amp = 0.05; // smoothed swell
    let ring = 0;
    let spin = 0;
    let last = performance.now();

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const r = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(r.width * dpr));
      canvas.height = Math.max(1, Math.round(r.height * dpr));
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const t = now / 1000;
      const st = stateRef.current;
      const w = canvas.width;
      const h = canvas.height;
      const x0 = w / 2;
      const y0 = h * 0.47;
      const r0 = Math.min(w, h) * 0.3;

      // How much the orb swells: the voice level when speaking, a gentle breath otherwise.
      let target = 0.05 + 0.025 * Math.sin(t * 1.6);
      if (st === "speaking") {
        const lv = levelRef.current?.() ?? 0.4;
        target = 0.08 + Math.min(1, lv) * 0.42;
      } else if (st === "listening") target = 0.1 + 0.04 * Math.sin(t * 4);
      else if (st === "thinking") target = 0.07;
      if (reduced) target *= 0.4;
      amp += (target - amp) * Math.min(1, dt * (st === "speaking" ? 14 : 4));
      spin += dt * (st === "thinking" ? 1.6 : st === "speaking" ? 1.1 : 0.45) * (reduced ? 0.3 : 1);
      ring += dt;

      cx.clearRect(0, 0, w, h);

      // Halo
      const halo = cx.createRadialGradient(x0, y0, r0 * 0.6, x0, y0, r0 * (1.9 + amp * 1.5));
      halo.addColorStop(0, `rgba(56, 180, 170, ${0.22 + amp * 0.45})`);
      halo.addColorStop(1, "rgba(56, 180, 170, 0)");
      cx.fillStyle = halo;
      cx.fillRect(0, 0, w, h);

      // Glass sphere
      const R = r0 * (1 + amp * 0.35);
      const body = cx.createRadialGradient(x0 - R * 0.25, y0 - R * 0.3, R * 0.05, x0, y0, R);
      body.addColorStop(0, "#1b3a40");
      body.addColorStop(0.7, "#0c1d22");
      body.addColorStop(1, "#071216");
      cx.save();
      cx.beginPath();
      cx.arc(x0, y0, R, 0, Math.PI * 2);
      cx.fillStyle = body;
      cx.fill();
      cx.clip();

      // Swirling lobes inside the sphere, added together so they glow where they overlap
      cx.globalCompositeOperation = "screen";
      for (const lobe of LOBES) {
        const n = 90;
        cx.beginPath();
        for (let i = 0; i <= n; i++) {
          const th = (i / n) * Math.PI * 2;
          const wob = Math.sin(lobe.k[0] * th + spin * lobe.speed * 2 + lobe.phase) * 0.5
            + Math.sin(lobe.k[1] * th - spin * lobe.speed * 1.3 + lobe.phase * 0.7) * 0.3
            + Math.sin(lobe.k[2] * th + t * lobe.speed + lobe.phase) * 0.2;
          const rr = R * (0.4 + amp * 0.5) * (1 + wob * (0.2 + amp * 0.9));
          const ox = Math.cos(spin * lobe.speed + lobe.phase) * R * 0.3;
          const oy = Math.sin(spin * lobe.speed * 0.8 + lobe.phase) * R * 0.26;
          const px = x0 + ox + Math.cos(th) * rr;
          const py = y0 + oy + Math.sin(th) * rr;
          if (i === 0) cx.moveTo(px, py);
          else cx.lineTo(px, py);
        }
        cx.closePath();
        const [r, g, b] = lobe.color;
        const fill = cx.createRadialGradient(x0, y0, 0, x0, y0, R * 1.1);
        fill.addColorStop(0, `rgba(${r},${g},${b},${0.38 + amp * 0.3})`);
        fill.addColorStop(0.75, `rgba(${r},${g},${b},${0.16 + amp * 0.16})`);
        fill.addColorStop(1, `rgba(${r},${g},${b},0)`);
        cx.fillStyle = fill;
        cx.fill();
      }
      cx.globalCompositeOperation = "source-over";
      cx.restore();

      // Specular highlight and rim
      const spec = cx.createRadialGradient(x0 - R * 0.35, y0 - R * 0.45, 0, x0 - R * 0.35, y0 - R * 0.45, R * 0.6);
      spec.addColorStop(0, "rgba(255,255,255,0.32)");
      spec.addColorStop(1, "rgba(255,255,255,0)");
      cx.fillStyle = spec;
      cx.beginPath();
      cx.arc(x0, y0, R, 0, Math.PI * 2);
      cx.fill();
      cx.lineWidth = Math.max(1, R * 0.015);
      cx.strokeStyle = `rgba(120, 230, 215, ${0.25 + amp * 0.5})`;
      cx.stroke();

      // Listening: a ring pulsing outwards
      if (st === "listening") {
        const p = (ring % 1.4) / 1.4;
        cx.beginPath();
        cx.arc(x0, y0, R * (1.08 + p * 0.45), 0, Math.PI * 2);
        cx.lineWidth = Math.max(1.5, R * 0.03);
        cx.strokeStyle = `rgba(46, 107, 94, ${0.55 * (1 - p)})`;
        cx.stroke();
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  const label = state === "speaking" ? "AI assistant, speaking" : state === "listening" ? "AI assistant, listening"
    : state === "thinking" ? "AI assistant, thinking" : "AI assistant";
  return <canvas ref={ref} className="voice-orb" role="img" aria-label={label} />;
}
