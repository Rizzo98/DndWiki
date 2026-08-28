'''Regression tests for VoiceEmbedder output-shape + HF token handling.

Enrollment 500 bug: a stereo/multichannel upload made SpeechBrain's
``encode_batch`` return several 192-d embeddings (torchaudio.load yields
``[channels, samples]`` and SpeechBrain treats channels as a batch). The
nested list was upserted to Qdrant as a multi-dense vector, which the
regular 192-d 'voiceprints' collection rejects with a 400.

``embed_bytes`` now downmixes to mono and resamples to the model's 16 kHz
before embedding (a 48 kHz upload fed as-is lands in a different embedding
space and never matches the 16 kHz session-derived prints) and flattens via
``_flatten_embedding``. torch/speechbrain are not installed in the dev
venv, so the pure flattening contract, the HF-token plumbing and the
resample behaviour are tested with fakes here.
'''

import asyncio
import os
import sys
import types

from app.core.config import ServiceSettings
from app.embedding import VoiceEmbedder, _flatten_embedding


def _fake_speechbrain(monkeypatch, capture: dict):
    '''Inject fake speechbrain modules (EncoderClassifier + FetchConfig).'''
    sb = types.ModuleType('speechbrain')
    sb_inference = types.ModuleType('speechbrain.inference')
    sb_speaker = types.ModuleType('speechbrain.inference.speaker')
    sb_utils = types.ModuleType('speechbrain.utils')
    sb_fetching = types.ModuleType('speechbrain.utils.fetching')

    class FetchConfig:
        def __init__(self, token=False):
            self.token = token

    class FakeEmbedding:
        def squeeze(self, dim=None):
            return self

        def tolist(self):
            return [[0.1] * 192]

    class FakeEncoder:
        def encode_batch(self, wavs):
            return FakeEmbedding()

    class FakeEncoderClassifier:
        @classmethod
        def from_hparams(cls, **kwargs):
            capture.update(kwargs)
            return FakeEncoder()

    sb_speaker.EncoderClassifier = FakeEncoderClassifier
    sb_fetching.FetchConfig = FetchConfig
    monkeypatch.setitem(sys.modules, 'speechbrain', sb)
    monkeypatch.setitem(sys.modules, 'speechbrain.inference', sb_inference)
    monkeypatch.setitem(sys.modules, 'speechbrain.inference.speaker', sb_speaker)
    monkeypatch.setitem(sys.modules, 'speechbrain.utils', sb_utils)
    monkeypatch.setitem(sys.modules, 'speechbrain.utils.fetching', sb_fetching)


# ------------------------------------------------------------ flattening


def test_flat_embedding_passes_through():
    emb = [0.1] * 192
    assert _flatten_embedding(emb) == emb


def test_multiple_rows_are_averaged_into_one():
    assert _flatten_embedding([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]) == [2.0, 3.0, 4.0]


def test_scalar_output_is_wrapped():
    assert _flatten_embedding(0.5) == [0.5]


def test_empty_input_stays_empty():
    assert _flatten_embedding([]) == []


# ------------------------------------------------------------ HF token


def test_load_classifier_passes_hf_token_and_sets_env(monkeypatch):
    capture: dict = {}
    _fake_speechbrain(monkeypatch, capture)
    monkeypatch.delenv('HF_TOKEN', raising=False)

    embedder = VoiceEmbedder(ServiceSettings(hf_token='hf-secret'))
    embedder._load_classifier()

    assert capture['source'] == 'speechbrain/spkrec-ecapa-voxceleb'
    assert capture['savedir'] == '/app/models'
    assert capture['fetch_config'].token is True
    assert os.environ.get('HF_TOKEN') == 'hf-secret'


def test_load_classifier_without_token_passes_none(monkeypatch):
    capture: dict = {}
    _fake_speechbrain(monkeypatch, capture)
    monkeypatch.delenv('HF_TOKEN', raising=False)

    # explicit hf_token='' so a real HF_TOKEN in the env or repo .env
    # (dnd_common Settings loads .env) cannot leak into this test
    embedder = VoiceEmbedder(ServiceSettings(hf_token=''))
    embedder._load_classifier()

    assert capture['fetch_config'].token is False
    assert 'HF_TOKEN' not in os.environ


# ------------------------------------------------------------ 16 kHz resample


def _fake_torchaudio(monkeypatch, sr: int, channels: int, resamples: list):
    '''Fake torchaudio: returns a [channels, 16000] waveform at ``sr`` and
    records every Resample(sr -> dst) instantiation.'''
    ta = types.ModuleType('torchaudio')
    ta_transforms = types.ModuleType('torchaudio.transforms')

    class FakeResample:
        def __init__(self, src, dst):
            resamples.append((src, dst))

        def __call__(self, signal):
            return signal

    class FakeTensor:
        def __init__(self, shape):
            self.shape = shape

        def mean(self, dim=0, keepdim=False):
            return FakeTensor([1, self.shape[1]])

        def squeeze(self, dim=None):
            return self

        def tolist(self):
            return [[0.1] * 192]

    def fake_load(path):
        return FakeTensor([channels, 16000]), sr

    ta.load = fake_load
    ta_transforms.Resample = FakeResample
    ta.transforms = ta_transforms
    monkeypatch.setitem(sys.modules, 'torchaudio', ta)
    monkeypatch.setitem(sys.modules, 'torchaudio.transforms', ta_transforms)


def test_embed_bytes_resamples_native_rate_to_16k(monkeypatch):
    '''Regression: a 48 kHz enrollment clip must be resampled to the model's
    16 kHz, or the print lives in a different embedding space and never
    matches the 16 kHz session-derived prints (cosine ~0).'''
    capture: dict = {}
    _fake_speechbrain(monkeypatch, capture)
    resamples: list = []
    _fake_torchaudio(monkeypatch, sr=48000, channels=2, resamples=resamples)

    embedder = VoiceEmbedder(ServiceSettings(hf_token=''))
    emb, dur = asyncio.run(embedder.embed_bytes(b'fake-audio', suffix='.mp3'))

    assert resamples == [(48000, 16000)]
    assert dur == 16000 / 48000
    assert len(emb) == 192


def test_embed_bytes_skips_resample_at_16k(monkeypatch):
    capture: dict = {}
    _fake_speechbrain(monkeypatch, capture)
    resamples: list = []
    _fake_torchaudio(monkeypatch, sr=16000, channels=1, resamples=resamples)

    embedder = VoiceEmbedder(ServiceSettings(hf_token=''))
    emb, dur = asyncio.run(embedder.embed_bytes(b'fake-audio', suffix='.wav'))

    assert resamples == []
    assert dur == 1.0
    assert len(emb) == 192
