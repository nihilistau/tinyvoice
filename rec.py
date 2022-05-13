#!/usr/bin/env python3
import os
import time
import csv
import torch
from tqdm.auto import tqdm
from torch import log_softmax, nn
import torch.optim as optim
import torchaudio
import numpy as np
from torch.utils.data import Dataset

DATASET = "/raid/ljspeech/LJSpeech-1.1"
CHARSET = " abcdefghijklmnopqrstuvwxyz,."
XMAX = 870    # about 10 seconds
YMAX = 150
SAMPLE_RATE = 22050

import functools
@functools.lru_cache(None)
def get_metadata():
  ret = []
  with open(os.path.join(DATASET, 'metadata.csv'), newline='') as csvfile:
    reader = csv.reader(csvfile, delimiter='|')
    for row in reader:
      answer = [CHARSET.index(c)+1 for c in row[1].lower() if c in CHARSET]
      if len(answer) <= YMAX:
        ret.append((os.path.join(DATASET, 'wavs', row[0]+".wav"), answer))
  return ret

mel_transform = torchaudio.transforms.MelSpectrogram(SAMPLE_RATE, n_fft=1024, win_length=1024, hop_length=256, n_mels=80)
def load_example(x):
  waveform, sample_rate = torchaudio.load(x, normalize=True)
  assert(sample_rate == SAMPLE_RATE)
  mel_specgram = mel_transform(waveform)
  #return 10*torch.log10(mel_specgram[0]).T
  return mel_specgram[0].T

cache = {}
def init_data():
  meta = get_metadata()
  for x,y in tqdm(meta):
    cache[x] = load_example(x), y
init_data()

import hashlib
class LJSpeech(Dataset):
  def __init__(self, val=False):
    self.meta = get_metadata()
    if val:
      cmp = lambda x: hashlib.sha1(x[0].encode('utf-8')).hexdigest()[0] == '0'
    else:
      cmp = lambda x: hashlib.sha1(x[0].encode('utf-8')).hexdigest()[0] != '0'
    self.meta = [x for x in self.meta if cmp(x)]
    print(f"set has {len(self.meta)}")

  def __len__(self):
    return len(self.meta)

  def __getitem__(self, idx):
    x,y = self.meta[idx]
    if x not in cache:
      print("NEVER SHOULD HAPPEN")
      cache[x] = load_example(x), y
    return cache[x]

class TemporalBatchNorm(nn.Module):
  def __init__(self, channels):
    super().__init__()
    self.bn = nn.BatchNorm1d(channels)

  def forward(self, x):
    # (L, N, C)
    xx = x.permute(1,2,0)
    # (N, C, L)
    xx = self.bn(xx)
    xx = xx.permute(2,0,1)
    #print(x.shape, xx.shape)
    #return self.bn(x.permute(1,2,0)).permute(1,2,0)
    return xx

class Rec(nn.Module):
  def __init__(self):
    super().__init__()
    H = 256
    # (L, N, C)
    self.prepare = nn.Sequential(
      nn.Linear(80, H),
      TemporalBatchNorm(H),
      nn.ReLU(),
      nn.Linear(H, H),
      TemporalBatchNorm(H),
      nn.ReLU())
    self.encoder = nn.GRU(H, H, batch_first=False)
    self.decode = nn.Sequential(
      nn.Linear(H, H//2),
      TemporalBatchNorm(H//2),
      nn.ReLU(),
      nn.Linear(H//2, len(CHARSET))
    )

  def forward(self, x):
    x = self.prepare(x)
    x = nn.functional.relu(self.encoder(x)[0])
    x = self.decode(x)
    return torch.nn.functional.log_softmax(x, dim=2)

def pad_sequence(batch):
  sorted_batch = sorted(batch, key=lambda x: x[0].shape[0], reverse=True)
  input_lengths = [x[0].shape[0] for x in sorted_batch]
  #input_lengths = [sorted_batch[0][0].shape[0] for x in sorted_batch]
  target_lengths = [len(x[1]) for x in sorted_batch]
  sequences = [x[0] for x in sorted_batch]
  sequences_padded = torch.nn.utils.rnn.pad_sequence(sequences, batch_first=False)
  labels = sum([x[1] for x in sorted_batch], [])
  labels = torch.tensor(labels, dtype=torch.int32)
  #labels = [x[1]+[0]*(YMAX - len(x[1])) for x in sorted_batch]
  #labels = torch.LongTensor(labels)
  #labels = labels[:, :max(target_lengths)]
  return sequences_padded, labels, input_lengths, target_lengths

def get_dataloader(batch_size, val):
  dset = LJSpeech(val)
  trainloader = torch.utils.data.DataLoader(dset, batch_size=batch_size, shuffle=True, num_workers=2 if val else 8, collate_fn=pad_sequence, pin_memory=True)
  return dset, trainloader

import wandb

WAN = True
def train():
  if WAN:
    wandb.init(project="tinyvoice", entity="geohot")

  epochs = 300
  learning_rate = 3e-4
  batch_size = 128
  wandb.config = {
    "learning_rate": learning_rate,
    "epochs": epochs,
    "batch_size": batch_size
  }

  timestamp = int(time.time())
  dset, trainloader = get_dataloader(batch_size, False)
  valdset, valloader = get_dataloader(batch_size, True)
  ctc_loss = nn.CTCLoss().cuda()
  model = Rec().cuda()
  model.load_state_dict(torch.load('models/tinyvoice_1652472269_7.pt'))

  #optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9)
  import apex
  optimizer = apex.optimizers.FusedAdam(model.parameters(), lr=learning_rate)
  #optimizer = optim.Adam(model.parameters(), lr=learning_rate)
  val = torch.tensor(load_example('data/LJ037-0171.wav')).cuda()
  for epoch in range(epochs):
    if WAN:
      wandb.watch(model)

    if epoch%2 == 0:
      mguess = model(val[:, None])
      pp = ''.join([CHARSET[c-1] for c in mguess[:, 0, :].argmax(dim=1).cpu() if c != 0])
      print("VALIDATION", pp)
      torch.save(model.state_dict(), f"models/tinyvoice_{timestamp}_{epoch}.pt")

    t = tqdm(valloader, total=len(valdset)//batch_size)
    losses = []
    for data in t:
      input, target, input_lengths, target_lengths = data
      input = input.to('cuda:0', non_blocking=True)
      target = target.to('cuda:0', non_blocking=True)
      guess = model(input)
      loss = ctc_loss(guess, target, input_lengths, target_lengths)
      losses.append(loss)
    val_loss = torch.mean(torch.tensor(losses)).item()
    print(f"val_loss: {val_loss:.2f}")
    if WAN:
      wandb.log({"val_loss": val_loss})

    t = tqdm(trainloader, total=len(dset)//batch_size)
    for data in t:
      input, target, input_lengths, target_lengths = data
      input = input.to('cuda:0', non_blocking=True)
      target = target.to('cuda:0', non_blocking=True)
      optimizer.zero_grad()
      guess = model(input)
      #print(input)
      #print(guess)
      #print(target)
      #print(guess.shape, target.shape, input_lengths, target_lengths)

      """
      pp = ''.join([CHARSET[c-1] for c in guess[:, 0, :].argmax(dim=1).cpu() if c != 0])
      if len(pp) > 0:
        print(pp)
      """

      loss = ctc_loss(guess, target, input_lengths, target_lengths)
      #print(loss)
      #loss = loss.mean()
      loss.backward()
      optimizer.step()
      t.set_description("loss: %.2f" % loss.item())
      if WAN:
        wandb.log({"loss": loss})

if __name__ == "__main__":
  train()
