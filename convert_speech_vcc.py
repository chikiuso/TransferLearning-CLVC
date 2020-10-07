import os
import math
import h5py
import time
import torch
import librosa
import argparse
import numpy as np
from scipy.io.wavfile import read, write

from denoiser import Denoiser
from train_ppg2mel_spk import *
from prepare_h5 import frame_inference
from common.hparams_spk import create_hparams
from common.model import Tacotron2_multispeaker
from spk_embedder.embedder import SpeechEmbedder
from mel2samp import files_to_list, MAX_WAV_VALUE


torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True


parser = argparse.ArgumentParser()
parser.add_argument('-ch', "--checkpoint_path", type=str, required=True)
parser.add_argument('-wg', "--waveglow", type=str, required=True)
parser.add_argument('-m', "--model", type=str, required=True)
parser.add_argument('-s', "--sigma", type=float, default=0.8)
parser.add_argument('-o', "--outputs", type=str, required=True)
parser.add_argument("--sampling_rate", type=int, default=24000)
parser.add_argument("--cuda", type=bool, default=True)
parser.add_argument("--is_fp16", type=bool, default=True)
parser.add_argument('-f', '--h5_feature_path', type=str, default="VCC_features.h5")
parser.add_argument('-vcc', '--vcc_root_path', type=str, default="testing")
parser.add_argument("-d", "--denoiser_strength", type=float, default=0.08, help='Removes model bias.')
args = parser.parse_args()
os.makedirs(args.outputs, exist_ok=True)


### load ppg model
model = torch.jit.load(args.model).eval()

### load ppg2mel model
hparams = create_hparams()
torch.manual_seed(hparams.seed)
torch.cuda.manual_seed(hparams.seed)
ppg2mel_model = Tacotron2_multispeaker(hparams)
if args.checkpoint_path is not None:
	ppg2mel_model.load_state_dict(torch.load(args.checkpoint_path)['state_dict'])
ppg2mel_model.cuda().eval()

### load waveglow model
waveglow = torch.load(args.waveglow)['model']
waveglow = waveglow.remove_weightnorm(waveglow)
waveglow = waveglow.cuda().eval()
if args.is_fp16:
	from apex import amp
	waveglow, _ = amp.initialize(waveglow, [], opt_level="O3")
if args.denoiser_strength > 0:
	denoiser = Denoiser(waveglow).cuda()


h5 = h5py.File(args.h5_feature_path, "r")
wav_root_path = args.vcc_root_path
spks = ['chinese', 'english']
for sid in range(0, 4):
	source_name = spks[sid]
	source_folder = os.path.join(wav_root_path, source_name)
	wavnames = os.listdir(source_folder)
	for wi, wavname in enumerate(wavnames):
		wavpath = os.path.join(source_folder, wavname)
		source, _ = librosa.load(wavpath, 16000)
		source = source * 32768
		source = np.concatenate((np.zeros([512 + 160 * (7 - 3), ]),
		                         source,
		                         np.zeros([512 + 160 * (7 - 3), ])))
		amp = 32768 * 0.8 / np.max(np.abs(source))

		ppg = frame_inference(wavpath, model, use_cuda=args.cuda, sig=source).cuda().detach()  # (T1, D)
		zcr = np.zeros([math.ceil(len(source) / 160), ], dtype=np.float32)
		log_energy = np.zeros([math.ceil(len(source) / 160), ], dtype=np.float32)
		for frame_id, seg_start in enumerate(range(0, len(source), 160)):
			if seg_start + 512 > len(source):
				break
			seg = source[seg_start: seg_start + 512] / 32768.0
			log_energy[frame_id] = np.log(amp ** 0.5 * np.sum(seg ** 2) + 1e-8)
			sign_seg = np.sign(seg)
			zcr[frame_id] = np.mean(sign_seg[:-1] != sign_seg[1:])
		zcr = zcr[7:frame_id - 7]
		log_energy = log_energy[7:frame_id - 7]
		if log_energy.shape[0] != ppg.shape[0]:
			print("Error. Frame length mismatched. {} != {}".format(ppg.shape[0], log_energy.shape[0]))
			exit()
		ppg = torch.cat((ppg,
		                 torch.log(torch.FloatTensor(zcr).cuda() + 1e-8).unsqueeze(1),
		                 torch.FloatTensor(log_energy).cuda().unsqueeze(1)), dim=-1)
		ppg = ppg.unsqueeze(0).transpose(1, 2)

		for tid in range(4, 14):
			target_name = spks[tid]
			dvec = torch.mean(torch.FloatTensor(h5[str(tid)]["dvec"][:]), dim=0).cuda()

			print("Source: {}, {}/{}, Wav: {}, Target: {}     ".format(source_name, wi+1, len(wavnames), wavname, target_name), end="\r")
			with torch.no_grad():
				y_pred = ppg2mel_model.inference(ppg, dvec.reshape(1, -1, 1).repeat(1, 1, ppg.shape[2]))
				mel_outputs_before_postnet, mel, gate_outputs, alignments = y_pred
				mel = mel.half() if args.is_fp16 else mel
				audio = waveglow.infer(mel, sigma=args.sigma)
				if args.denoiser_strength > 0:
					audio = denoiser(audio.cuda(), args.denoiser_strength)
				audio = audio * MAX_WAV_VALUE

			audio = audio.squeeze().cpu().numpy().astype('int16')
			audio_path = os.path.join(args.outputs, "{}_{}_{}".format(target_name, source_name, wavname))
			write(audio_path, args.sampling_rate, audio)

print()



if __name__ == "__main__":
	pass
