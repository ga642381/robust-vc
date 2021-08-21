"""Dataset for reconstruction scheme."""

import json
import random
from pathlib import Path
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import torch

# from torch._C import device
from tqdm import tqdm
from torch.utils.data import Dataset
from torch.nn.utils.rnn import pad_sequence

from .feature_extract import FeatureExtractor
from .noise import WavAug
from .utils import load_wav, log_mel_spectrogram


class IntraSpeakerDataset(Dataset):
    """Dataset for reconstruction scheme.

    Returns:
        speaker_id: speaker id number.
        feat: Wav2Vec feature tensor.
        mel: log mel spectrogram tensor.
    """

    def __init__(
        self,
        split_type: str,
        split_spks: list,
        data_dir,
        metadata_path,
        src_feat,
        ref_feat,
        n_samples=5,
        pre_load=False,
        training=True,
    ):
        with open(metadata_path, "r") as f:
            metadata = json.load(f)

        executor = ThreadPoolExecutor(max_workers=4)
        futures = []
        for speaker_name, utterances in metadata.items():
            if speaker_name in split_spks:  # split speakers here
                for utterance in utterances:
                    futures.append(
                        executor.submit(
                            _process_data,
                            speaker_name,
                            data_dir,
                            utterance,
                            pre_load,
                            src_feat,
                            ref_feat,
                        )
                    )

        self.data = []
        self.speaker_to_indices = {}
        for i, future in enumerate(tqdm(futures, ncols=0)):
            result = future.result()
            speaker_name = result[0]
            self.data.append(result)
            if speaker_name not in self.speaker_to_indices:
                self.speaker_to_indices[speaker_name] = [i]
            else:
                self.speaker_to_indices[speaker_name].append(i)

        self.split_type = split_type
        self.split_spks = split_spks
        self.data_dir = Path(data_dir)
        self.n_samples = n_samples
        self.pre_load = pre_load
        self.training = training
        self.src_feat = src_feat
        self.ref_feat = ref_feat
        self.src_dim = -1
        self.ref_dim = -1
        self.tgt_dim = -1

        # === add noise === #
        self.src_feat_extractor = FeatureExtractor(src_feat, None, device="cpu")
        self.ref_feat_extractor = FeatureExtractor(ref_feat, None, device="cpu")
        self.wavaug = WavAug()

    def __len__(self):
        return len(self.data)

    # __getitem__ <-- _get_data <-- _load_data
    def __getitem__(self, index):
        # speaker_name, content_emb, target_emb, target_mel = self._get_data(index)
        _, content_wav, target_wav, target_mel = self._get_data(index)
        # return content_emb, target_emb, target_mel
        return content_wav, target_wav, target_mel

    def _get_data(self, index):
        if self.pre_load:
            speaker_name, content_wav, _, target_wav, _, target_mel = self.data[index]
        else:
            # noisy training try this first
            speaker_name, content_wav, _, target_wav, _, target_mel = _load_data(
                *self.data[index]
            )
        # === add noise === #
        # wav -> wav (noisy) -> cpc
        if self.src_feat == self.ref_feat:
            # same noisy wav and ssl feature for training
            content_noisy_wav = self.wavaug.add_noise(content_wav)
            content_emb = (
                self.src_feat_extractor.get_feature(content_noisy_wav)[0].detach().cpu()
            )
            target_emb = content_emb
        else:
            # more general training scheme
            content_noisy_wav = self.wavaug.add_noise(content_wav)
            target_noisy_wav = self.wavaug.add_noise(target_wav)
            content_emb = (
                self.src_feat_extractor.get_feature(content_noisy_wav)[0].detach().cpu()
            )
            target_emb = (
                self.ref_feat_extractor.get_feature(target_noisy_wav)[0].detach().cpu()
            )

        self.src_dim = content_emb.shape[1]
        self.ref_dim = target_emb.shape[1]
        self.tgt_dim = target_mel.shape[1]

        return speaker_name, content_emb, target_emb, target_mel

    def get_feat_dim(self):
        self._get_data(0)
        return self.src_dim, self.ref_dim, self.tgt_dim


def _process_data(speaker_name, data_dir, feature, pre_load, src_feat, ref_feat):
    _, src_feature_path, ref_feature_path = (
        feature["audio_path"],
        feature[src_feat],
        feature[ref_feat],
    )
    if pre_load:
        return _load_data(speaker_name, data_dir, src_feature_path, ref_feature_path)
    else:
        return speaker_name, data_dir, src_feature_path, ref_feature_path


def _load_data(speaker_name, data_dir, src_feature_path, ref_feature_path):
    # feature : {"feat": tensor, "wav" : tensor, "mel" : tensor}
    src_feature = torch.load(Path(data_dir, src_feature_path), "cpu")
    ref_feature = torch.load(Path(data_dir, ref_feature_path), "cpu")

    content_wav = src_feature["wav"].detach().cpu()
    content_emb = src_feature["feat"].detach().cpu()

    target_wav = ref_feature["wav"].detach().cpu()
    target_emb = ref_feature["feat"].detach().cpu()

    target_mel = src_feature["mel"].detach().cpu()
    return speaker_name, content_wav, content_emb, target_wav, target_emb, target_mel


# ==== collate function === #
def collate_batch(batch):
    """Collate a batch of data."""
    """
    srcs      : (batch, max_src_len, feat_dim)
    src_masks : (batch, max_src_len) //False if value

    tgts      : (batch, feat_dim, max_tgt_len)
    tgt_masks : (batch, max_tgt_len) // False if value

    tgt_mels      : (batch, mel_dim, max_tgt_mel_len)
    overlap_lens  : list, len == batch_size 
    """
    srcs, tgts, tgt_mels = zip(*batch)

    src_lens = [len(src) for src in srcs]
    tgt_lens = [len(tgt) for tgt in tgts]
    tgt_mel_lens = [len(tgt_mel) for tgt_mel in tgt_mels]

    overlap_lens = [
        min(src_len, tgt_mel_len)
        for src_len, tgt_mel_len in zip(src_lens, tgt_mel_lens)
    ]

    srcs = pad_sequence(srcs, batch_first=True)

    src_masks = [torch.arange(srcs.size(1)) >= src_len for src_len in src_lens]
    src_masks = torch.stack(src_masks)

    tgts = pad_sequence(tgts, batch_first=True, padding_value=-20)
    tgts = tgts.transpose(1, 2)  # (batch, mel_dim, max_tgt_len)

    tgt_masks = [torch.arange(tgts.size(2)) >= tgt_len for tgt_len in tgt_lens]
    tgt_masks = torch.stack(tgt_masks)  # (batch, max_tgt_len)

    tgt_mels = pad_sequence(tgt_mels, batch_first=True, padding_value=-20)
    tgt_mels = tgt_mels.transpose(1, 2)  # (batch, mel_dim, max_tgt_len)

    return srcs, src_masks, tgts, tgt_masks, tgt_mels, overlap_lens
