import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.audio.capture import Segmenter


def bench(model_size, beam):
    from faster_whisper import WhisperModel

    track = np.load(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "video_track.npy")
    seg = Segmenter()
    chunks = []
    step = 480
    for i in range(0, len(track), step):
        got = seg.feed(track[i:i + step].astype(np.float32))
        if got is not None:
            chunks.append(got)

    t0 = time.time()
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    load_s = time.time() - t0

    audio_warm = np.zeros(16000, dtype=np.float32)
    list(model.transcribe(audio_warm, beam_size=1)[0])

    total_audio = 0.0
    total_proc = 0.0
    n_words = 0
    for audio in chunks:
        dur = len(audio) / 16000.0
        t1 = time.time()
        segs, info = model.transcribe(audio, beam_size=beam, best_of=beam,
                                      condition_on_previous_text=False)
        text = "".join(s.text.strip() for s in segs if s.text.strip())
        dt = time.time() - t1
        total_audio += dur
        total_proc += dt
        n_words += len(text.split())
        print(f"    seg {dur:5.1f}s -> {dt:5.2f}s (RTF {dt/dur:.2f}) {text[:44]}")
    rtf = total_proc / total_audio if total_audio else 0
    print(f"BENCH model={model_size} beam={beam}: load={load_s:.1f}s, "
          f"audio={total_audio:.1f}s, proc={total_proc:.1f}s, RTF={rtf:.2f}, words={n_words}")
    return rtf


if __name__ == "__main__":
    size = sys.argv[1] if len(sys.argv) > 1 else "base"
    for beam in (2, 1):
        bench(size, beam)
