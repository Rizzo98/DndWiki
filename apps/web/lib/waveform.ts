// Decorative waveform envelopes.
//
// The app does not compute real audio peaks — that needs an ffmpeg/audiowaveform
// pass in the pipeline writing a peaks artifact next to the transcript. Until
// then, a bar strip is drawn from a DETERMINISTIC seed (a session id), so the
// same session always gets the same shape and it never flickers when presigned
// URLs rotate. Everything real about a waveform here — progress, seek target,
// times — comes from the audio element, not from these numbers.

/**
 * Bar heights in [0.12, 1] for a seed. The slow envelope under the noise keeps
 * neighbouring bars related, and the ends taper so the strip reads as a
 * recording rather than a bar chart.
 */
export function waveformPeaks(seed: string, count: number): number[] {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i += 1) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const peaks: number[] = [];
  for (let i = 0; i < count; i += 1) {
    h ^= h << 13;
    h ^= h >>> 17;
    h ^= h << 5;
    const noise = ((h >>> 0) % 1000) / 1000;
    const shape = 0.45 + 0.55 * Math.sin((i / count) * Math.PI);
    const taper = Math.min(1, Math.min(i, count - 1 - i) / 6 + 0.25);
    peaks.push(Math.max(0.12, Math.min(1, (0.35 + 0.65 * noise) * shape * taper)));
  }
  return peaks;
}
