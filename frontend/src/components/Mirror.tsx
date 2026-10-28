import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type RefObject } from "react";

export interface MirrorHandle {
  capture: () => Promise<Blob | null>;
}

interface Props {
  mode: "avatar" | "camera";
  listening: boolean;
  speaking: boolean;
  handedOver: boolean;
  /** The talking avatar's canvas (src/avatar). Always mounted, so the avatar keeps its session while the camera is on. */
  avatarCanvasRef?: RefObject<HTMLCanvasElement>;
  /** Show the talking avatar; otherwise the abstract face (no avatar set up, still connecting, or unavailable). */
  showAvatar?: boolean;
}

// An embedded stream page can replace the abstract face too (VITE_AVATAR_STREAM_URL); the clinic's AI Avatar service
// (src/avatar) takes precedence when it is set up.
const AVATAR_URL = import.meta.env.VITE_AVATAR_STREAM_URL as string | undefined;

function AbstractFace({ speaking }: { speaking: boolean }) {
  return (
    <svg viewBox="0 0 120 150" aria-hidden>
      <ellipse cx="60" cy="72" rx="44" ry="58" fill="#f3d6c6" />
      <path d="M38 64 q8 -7 16 0" stroke="#1e2a2e" strokeWidth="3" fill="none" strokeLinecap="round" />
      <path d="M66 64 q8 -7 16 0" stroke="#1e2a2e" strokeWidth="3" fill="none" strokeLinecap="round" />
      <ellipse cx="60" cy="100" rx="11" ry={speaking ? 6 : 1.8} fill="#1e2a2e">
        {speaking && <animate attributeName="ry" values="2;7;3;6;2" dur="0.9s" repeatCount="indefinite" />}
      </ellipse>
    </svg>
  );
}

export const Mirror = forwardRef<MirrorHandle, Props>(function Mirror(
  { mode, listening, speaking, handedOver, avatarCanvasRef, showAvatar = false }, ref) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [camError, setCamError] = useState<string | null>(null);

  useEffect(() => {
    if (mode !== "camera") return;
    let cancelled = false;
    setCamError(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      setCamError("No camera available. Use \u201cUpload photo\u201d below instead.");
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 1280 } }, audio: false })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          videoRef.current.play().catch(() => undefined);
        }
      })
      .catch(() => setCamError("No camera available. Use \u201cUpload photo\u201d below instead."));
    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    };
  }, [mode]);

  useImperativeHandle(ref, () => ({
    capture: async () => {
      const v = videoRef.current;
      if (!v || !v.videoWidth) return null;
      const canvas = document.createElement("canvas");
      canvas.width = v.videoWidth;
      canvas.height = v.videoHeight;
      canvas.getContext("2d")!.drawImage(v, 0, 0);
      // The frame stays in memory only; it is uploaded and then discarded (no health data on the device).
      return new Promise((resolve) => canvas.toBlob((b) => resolve(b), "image/jpeg", 0.92));
    },
  }));

  const cls = ["mirror", listening && "listening", speaking && "speaking", handedOver && "handed"].filter(Boolean).join(" ");
  const avatarShown = mode !== "camera" && showAvatar;
  return (
    <div className={cls}>
      {avatarCanvasRef && (
        <canvas ref={avatarCanvasRef} className="talking-avatar" hidden={!avatarShown} aria-label="AI assistant" role="img" />
      )}
      {avatarShown ? null : mode === "camera" ? (
        <>
          <video ref={videoRef} className="selfie" playsInline muted aria-label="Camera preview" />
          <div className="guide" aria-hidden />
          {camError && <div className="avatar-face" style={{ padding: "2rem", textAlign: "center", zIndex: 4 }}>{camError}</div>}
        </>
      ) : AVATAR_URL ? (
        <iframe src={AVATAR_URL} title="AI assistant" allow="autoplay; microphone" />
      ) : (
        <div className="avatar-face"><AbstractFace speaking={speaking} /></div>
      )}
    </div>
  );
});
