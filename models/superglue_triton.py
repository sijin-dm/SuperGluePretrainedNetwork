# %BANNER_BEGIN%
# ---------------------------------------------------------------------
# %COPYRIGHT_BEGIN%
#
#  Magic Leap, Inc. ("COMPANY") CONFIDENTIAL
#
#  Unpublished Copyright (c) 2020
#  Magic Leap, Inc., All Rights Reserved.
#
# NOTICE:  All information contained herein is, and remains the property
# of COMPANY. The intellectual and technical concepts contained herein
# are proprietary to COMPANY and may be covered by U.S. and Foreign
# Patents, patents in process, and are protected by trade secret or
# copyright law.  Dissemination of this information or reproduction of
# this material is strictly forbidden unless prior written permission is
# obtained from COMPANY.  Access to the source code contained herein is
# hereby forbidden to anyone except current COMPANY employees, managers
# or contractors who have executed Confidentiality and Non-disclosure
# agreements explicitly covering such access.
#
# The copyright notice above does not evidence any actual or intended
# publication or disclosure  of  this source code, which includes
# information that is confidential and/or proprietary, and is a trade
# secret, of  COMPANY.   ANY REPRODUCTION, MODIFICATION, DISTRIBUTION,
# PUBLIC  PERFORMANCE, OR PUBLIC DISPLAY OF OR THROUGH USE  OF THIS
# SOURCE CODE  WITHOUT THE EXPRESS WRITTEN CONSENT OF COMPANY IS
# STRICTLY PROHIBITED, AND IN VIOLATION OF APPLICABLE LAWS AND
# INTERNATIONAL TREATIES.  THE RECEIPT OR POSSESSION OF  THIS SOURCE
# CODE AND/OR RELATED INFORMATION DOES NOT CONVEY OR IMPLY ANY RIGHTS
# TO REPRODUCE, DISCLOSE OR DISTRIBUTE ITS CONTENTS, OR TO MANUFACTURE,
# USE, OR SELL ANYTHING THAT IT  MAY DESCRIBE, IN WHOLE OR IN PART.
#
# %COPYRIGHT_END%
# ----------------------------------------------------------------------
# %AUTHORS_BEGIN%
#
#  Originating Authors: Paul-Edouard Sarlin
#
# %AUTHORS_END%
# --------------------------------------------------------------------*/
# %BANNER_END%

from copy import deepcopy
from pathlib import Path
from typing import List, Dict

import torch
from torch import nn


def MLP(channels: list, do_bn=True):
  """ Multi-layer perceptron """
  n = len(channels)
  layers = []
  for i in range(1, n):
    layers.append(
        nn.Conv1d(channels[i - 1], channels[i], kernel_size=1, bias=True))
    if i < (n-1):
      if do_bn:
        layers.append(nn.BatchNorm1d(channels[i]))
      layers.append(nn.ReLU())
  return nn.Sequential(*layers)


def normalize_keypoints(kpts, size: torch.Tensor):
  """ Normalize keypoints locations based on image image_shape"""
  # height, width = shape
  # size = torch.tensor([[width, height]], dtype=torch.float, device=kpts.device)
  size = size.to(kpts.device)
  center = size / 2
  scaling = size.max(1, keepdim=True).values * 0.7
  return (kpts - center[:, None, :]) / scaling[:, None, :]


class KeypointEncoder(torch.jit.ScriptModule):
  """ Joint encoding of visual appearance and location using MLPs"""

  def __init__(self, feature_dim, layers):
    super().__init__()
    self.encoder = MLP([3] + layers + [feature_dim])
    nn.init.constant_(self.encoder[-1].bias, 0.0)

  @torch.jit.script_method
  def forward(self, kpts, scores):
    inputs = [kpts.transpose(1, 2), scores.unsqueeze(1)]
    return self.encoder(torch.cat(inputs, dim=1))


def attention(query, key, value):
  dim = query.shape[1]
  scores = torch.einsum('bdhn,bdhm->bhnm', query, key) / dim**.5
  prob = torch.nn.functional.softmax(scores, dim=-1)
  return torch.einsum('bhnm,bdhm->bdhn', prob, value), prob


class MultiHeadedAttention(torch.jit.ScriptModule):
  """ Multi-head attention to increase model expressivitiy """
  prob: List[torch.Tensor]

  def __init__(self, num_heads: int, d_model: int):
    super().__init__()
    assert d_model % num_heads == 0
    self.dim = d_model // num_heads
    self.num_heads = num_heads
    self.merge = nn.Conv1d(d_model, d_model, kernel_size=1)
    self.proj = nn.ModuleList([deepcopy(self.merge) for _ in range(3)])
    self.prob = []

  @torch.jit.script_method
  def forward(self, query, key, value):
    batch_dim = query.size(0)
    query, key, value = [l(x).view(batch_dim, self.dim, self.num_heads, -1)
                         for l, x in zip(self.proj, (query, key, value))]
    x, prob = attention(query, key, value)
    self.prob.append(prob)
    return self.merge(x.contiguous().view(batch_dim, self.dim*self.num_heads, -1))


class AttentionalPropagation(torch.jit.ScriptModule):
  def __init__(self, feature_dim: int, num_heads: int):
    super().__init__()
    self.attn = MultiHeadedAttention(num_heads, feature_dim)
    self.mlp = MLP([feature_dim*2, feature_dim*2, feature_dim])
    nn.init.constant_(self.mlp[-1].bias, 0.0)

  @torch.jit.script_method
  def forward(self, x, source):
    message = self.attn(x, source, source)
    return self.mlp(torch.cat([x, message], dim=1))


class AttentionalGNN(torch.jit.ScriptModule):
  def __init__(self, feature_dim: int, layer_names: list):
    super().__init__()
    self.layers = nn.ModuleList([
        AttentionalPropagation(feature_dim, 4)
        for _ in range(len(layer_names))])
    self.names = layer_names

  @torch.jit.script_method
  def forward(self, desc0, desc1):
    for i, layer in enumerate(self.layers):
      layer.attn.prob = []
      if self.names[i] == 'cross':
        src0, src1 = desc1, desc0
      else:  # if name == 'self':
        src0, src1 = desc0, desc1
      delta0, delta1 = layer(desc0, src0), layer(desc1, src1)
      desc0, desc1 = (desc0 + delta0), (desc1 + delta1)
    return desc0, desc1


def log_sinkhorn_iterations(Z, log_mu, log_nu, iters: int):
  """ Perform Sinkhorn Normalization in Log-space for stability"""
  u, v = torch.zeros_like(log_mu), torch.zeros_like(log_nu)
  for _ in range(iters):
    u = log_mu - torch.logsumexp(Z + v.unsqueeze(1), dim=2)
    v = log_nu - torch.logsumexp(Z + u.unsqueeze(2), dim=1)
  return Z + u.unsqueeze(2) + v.unsqueeze(1)


def log_optimal_transport(scores, alpha, iters: int):
  """ Perform Differentiable Optimal Transport in Log-space for stability"""
  b, m, n = scores.shape
  ms, ns = torch.tensor(m).to(scores), torch.tensor(n).to(scores)

  bins0 = alpha.expand(b, m, 1)
  bins1 = alpha.expand(b, 1, n)
  alpha = alpha.expand(b, 1, 1)

  couplings = torch.cat([torch.cat([scores, bins0], -1),
                         torch.cat([bins1, alpha], -1)], 1)

  norm = - (ms + ns).log()
  log_mu = torch.cat([norm.expand(m), ns.log()[None] + norm])
  log_nu = torch.cat([norm.expand(n), ms.log()[None] + norm])
  log_mu, log_nu = log_mu[None].expand(b, -1), log_nu[None].expand(b, -1)

  Z = log_sinkhorn_iterations(couplings, log_mu, log_nu, iters)
  Z = Z - norm  # multiply probabilities by M+N
  return Z


def arange_like(x, dim: int):
  return torch.ones(x.shape[dim], dtype=x.dtype, device=x.device).cumsum(0) - 1


class SuperGlue(torch.jit.ScriptModule):
  """SuperGlue feature matching middle-end

  Given two sets of keypoints and locations, we determine the
  correspondences by:
    1. Keypoint Encoding (normalization + visual feature and location fusion)
    2. Graph Neural Network with multiple self and cross-attention layers
    3. Final projection layer
    4. Optimal Transport Layer (a differentiable Hungarian matching algorithm)
    5. Thresholding matrix based on mutual exclusivity and a match_threshold

  The correspondence ids use -1 to indicate non-matching points.

  Paul-Edouard Sarlin, Daniel DeTone, Tomasz Malisiewicz, and Andrew
  Rabinovich. SuperGlue: Learning Feature Matching with Graph Neural
  Networks. In CVPR, 2020. https://arxiv.org/abs/1911.11763

  """
  default_config = {
      'descriptor_dim': 256,
      'weights': 'indoor',
      'keypoint_encoder': [32, 64, 128, 256],
      'GNN_layers': ['self', 'cross'] * 9,
      'sinkhorn_iterations': 50,
      'match_threshold': 0.2,
  }

  def __init__(self, config):
    super().__init__()
    self.config = {**self.default_config, **config}

    self.descriptor_dim = self.config['descriptor_dim']
    self.weights = self.config['weights']
    self.keypoint_encoder = self.config['keypoint_encoder']
    self.GNN_layers = self.config['GNN_layers']
    self.sinkhorn_iterations = self.config['sinkhorn_iterations']
    self.match_threshold = self.config['match_threshold']

    self.kenc = KeypointEncoder(
        self.descriptor_dim, self.keypoint_encoder)

    self.gnn = AttentionalGNN(
        self.descriptor_dim, self.GNN_layers)

    self.final_proj = nn.Conv1d(
        self.descriptor_dim, self.descriptor_dim,
        kernel_size=1, bias=True)

    bin_score = torch.nn.Parameter(torch.tensor(1.))
    self.register_parameter('bin_score', bin_score)

    assert self.weights in ['indoor', 'outdoor']
    path = Path(__file__).parent
    path = path / 'weights/superglue_{}.pth'.format(self.weights)
    self.load_state_dict(torch.load(path))
    print('Loaded SuperGlue model (\"{}\" weights)'.format(
        self.weights))

  @torch.jit.script_method
  def forward(self, kpts0, kpts1, desc0, desc1, scores0, scores1, shape0, shape1):
    """Run SuperGlue on a pair of keypoints and descriptors"""
    desc0 = desc0.permute(0, 2, 1)
    desc1 = desc1.permute(0, 2, 1)

    if kpts0.shape[1] == 0 or kpts1.shape[1] == 0:  # no keypoints
      kshape0, kshape1 = kpts0.shape[:-1], kpts1.shape[:-1]
      return kpts0.new_full(kshape0, -1, dtype=torch.int), kpts0.new_zeros(shape0)


    # Keypoint normalization.
    kpts0 = normalize_keypoints(kpts0, shape0) # shape: 1,W,H
    kpts1 = normalize_keypoints(kpts1, shape1) # shape: 1,W,H

    # Keypoint MLP encoder.
    desc0 = desc0 + self.kenc(kpts0, scores0)
    desc1 = desc1 + self.kenc(kpts1, scores1)

    # Multi-layer Transformer network.
    desc0, desc1 = self.gnn(desc0, desc1)

    # Final MLP projection.
    mdesc0, mdesc1 = self.final_proj(desc0), self.final_proj(desc1)

    # Compute matching descriptor distance.
    scores = torch.einsum('bdn,bdm->bnm', mdesc0, mdesc1)
    scores = scores / self.descriptor_dim**.5

    # Run the optimal transport.
    scores = log_optimal_transport(
        scores, self.bin_score,
        iters=self.sinkhorn_iterations)

    # Get the matches with score above "match_threshold".
    max0, max1 = scores[:, :-1, :-1].max(2), scores[:, :-1, :-1].max(1)
    indices0, indices1 = max0.indices, max1.indices
    mutual0 = arange_like(indices0, 1)[None] == indices1.gather(1, indices0)
    # mutual1 = arange_like(indices1, 1)[None] == indices0.gather(1, indices1)
    zero = torch.tensor(0).to(scores)
    mscores0 = torch.where(mutual0, max0.values.exp(), zero)
    # mscores1 = torch.where(mutual1, mscores0.gather(1, indices1), zero)
    valid0 = mutual0 & (mscores0 > self.match_threshold)
    # valid1 = mutual1 & valid0.gather(1, indices1)
    indices0 = torch.where(valid0, indices0, torch.tensor(-1).to(indices0))
    # indices1 = torch.where(valid1, indices1, torch.tensor(-1).to(indices1))
    indices0 = indices0.float() # !for serving.
    return indices0,mscores0