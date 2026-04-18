from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
except ImportError:  # pragma: no cover
    np = None
    sd = None
    sf = None


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.frames: list["np.ndarray"] = []
        self.stream = None

    @property
    def available(self) -> bool:
        return np is not None and sd is not None and sf is not None

    def start(self) -> None:
        if not self.available:
            raise RuntimeError("Instale numpy, sounddevice y soundfile para grabar audio.")
        if self.stream is not None:
            return
        self.frames = []

        def callback(indata, frames, time, status):  # pragma: no cover
            if status:
                return
            self.frames.append(indata.copy())

        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            callback=callback,
        )
        self.stream.start()

    def stop(self) -> Path:
        if self.stream is None or not self.available:
            raise RuntimeError("No hay una grabación activa.")

        self.stream.stop()
        self.stream.close()
        self.stream = None

        if not self.frames:
            raise RuntimeError("No se capturó audio.")

        audio = np.concatenate(self.frames, axis=0)
        with NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            path = Path(tmp.name)
        sf.write(path, audio, self.sample_rate)
        self.frames = []
        return path
