import argparse
import csv
import os
import random
from functools import partial
from math import ceil, sqrt
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import List

import librosa
from pesq import pesq
from pystoi import stoi
from scipy.io import wavfile
from tqdm import tqdm


def cal_pesq(fs):
    clean_wav_path = fs[0]
    noisy_wav_path = fs[1]
    r1, clean_wav = wavfile.read(clean_wav_path)
    r2, noisy_wav = wavfile.read(noisy_wav_path)
    assert r1 == r2
    stoi_score = stoi(clean_wav, noisy_wav, r1)
    pesq_score = pesq(r1, clean_wav, noisy_wav, "wb")
    return pesq_score, stoi_score


def main(clean_dir, noisy_dir):
    clean_dir = Path(clean_dir).resolve()
    noisy_dir = Path(noisy_dir).resolve()
    assert clean_dir != noisy_dir, f"clean_dir and noisy_dir should not be the same!"
    assert clean_dir.exists(), f"{clean_dir} does not exist!"
    print(f"[INFO] Task : Calculating PESQ for speech enhancement")
    print(f"[INFO] clean_dir : {clean_dir}")
    print(f"[INFO] noisy_dir : {noisy_dir}")

    # clean and noisy wavs
    clean_wav_files = librosa.util.find_files(clean_dir)
    noisy_wav_files = [
        f.replace(str(clean_dir), str(noisy_dir)) for f in clean_wav_files
    ]

    # get clean, noisy wav file pairs
    clean_noisy_wav_files = []
    for i in range(len(clean_wav_files)):
        clean_wav_path = clean_wav_files[i]
        noisy_wav_path = noisy_wav_files[i]
        if os.path.exists(clean_wav_path) and os.path.exists(noisy_wav_path):
            clean_noisy_wav_files.append((clean_wav_path, noisy_wav_path))

    # main calculating pesq function
    pesq_scores = []
    stoi_scores = []
    n_files = len(clean_noisy_wav_files)

    N_processes = cpu_count()
    print(f"[INFO] Start multiprocessing with {N_processes} processes")
    with Pool(processes=N_processes) as pool:

        with tqdm(total=n_files) as pbar:
            for i, (pesq_score, stoi_score) in enumerate(
                pool.imap_unordered(cal_pesq, clean_noisy_wav_files)
            ):

                pesq_scores.append(pesq_score)
                stoi_scores.append(stoi_score)
                pbar.update()

    pesq_avg = sum(pesq_scores) / len(pesq_scores)
    stoi_avg = sum(stoi_scores) / len(stoi_scores)
    print(f"[RESULT] average PESQ of {n_files} wav files : {pesq_avg}")
    print(f"[RESULT] average PESQ of {n_files} wav files : {stoi_avg}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("clean_dir", type=Path)
    parser.add_argument("noisy_dir", type=Path)
    main(**vars(parser.parse_args()))
