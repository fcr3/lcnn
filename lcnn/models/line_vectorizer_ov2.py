import itertools
import random
from collections import defaultdict
import time

import numpy as np
import ray
import torch
import torch.nn as nn
import torch.nn.functional as F

from lcnn.config import M

FEATURE_DIM = 8


class LineVectorizer(nn.Module):
    def __init__(self, fc1, fc2, ie, device):
        super().__init__()

        lambda_ = torch.linspace(0, 1, M.n_pts0)[:, None]
        self.register_buffer("lambda_", lambda_)
        self.do_static_sampling = M.n_stc_posl + M.n_stc_negl > 0

        self.fc1 = fc1
        scale_factor = M.n_pts0 // M.n_pts1
        self.pooling = nn.MaxPool1d(scale_factor, scale_factor)
        self.fc2_unloaded = fc2
        self.fc2 = fc2
        self.ie = ie
        self.device_used = device
        self.loss = nn.BCEWithLogitsLoss(reduction="none")
        
        self.fc2_batch_size = 0
        
    def forward(self, result, input_dict):
        h = result["preds"]
        h["jmap"] = torch.from_numpy(h["jmap"]).float().to(self.device_used)
        h["joff"] = torch.from_numpy(h["joff"]).float().to(self.device_used)
        
        # for deducing input shape of fc1
        # print("fc1 expected input:", result["feature"].shape)
        
        x = self.fc1.infer({
            next(iter(self.fc1.inputs)): result["feature"]
        })[next(iter(self.fc1.outputs))]
        x = torch.from_numpy(x).float().to(self.device_used)
        n_batch, n_channel, row, col = x.shape

        xs, ys, fs, ps, idx, jcs = [], [], [], [], [0], []
        
        start1 = time.time()
        for i, meta in enumerate(input_dict["meta"]):
            p, label, feat, jc = self.sample_lines(
                meta, h["jmap"][i], h["joff"][i], input_dict["mode"]
            )
            # print("p.shape:", p.shape)
            # print("label", label.shape)
            ys.append(label)
            jcs.append(jc)
            ps.append(p)
            fs.append(feat)

            p = p[:, 0:1, :] * self.lambda_ + p[:, 1:2, :] * (1 - self.lambda_) - 0.5
            p = p.reshape(-1, 2)  # [N_LINE x N_POINT, 2_XY]
            px, py = p[:, 0].contiguous(), p[:, 1].contiguous()
            px0 = px.floor().clamp(min=0, max=127)
            py0 = py.floor().clamp(min=0, max=127)
            px1 = (px0 + 1).clamp(min=0, max=127)
            py1 = (py0 + 1).clamp(min=0, max=127)
            px0l, py0l, px1l, py1l = px0.long(), py0.long(), px1.long(), py1.long()

            # xp: [N_LINE, N_CHANNEL, N_POINT]
            xp = (
                (
                    x[i, :, px0l, py0l] * (px1 - px) * (py1 - py)
                    + x[i, :, px1l, py0l] * (px - px0) * (py1 - py)
                    + x[i, :, px0l, py1l] * (px1 - px) * (py - py0)
                    + x[i, :, px1l, py1l] * (px - px0) * (py - py0)
                )
                .reshape(n_channel, -1, M.n_pts0)
                .permute(1, 0, 2)
            )
            
            # for deducing input shape of pooling
            # print("pooling expected input:", xp.shape)
            # print(self.pooling)
            # print(xp.shape)
            xp = self.pooling(xp)
            # print(xp.shape)
            xs.append(xp)
            idx.append(idx[-1] + xp.shape[0])
            # print("idx", idx)
        
        stamp1 = time.time() - start1
        
        x, y = torch.cat(xs), torch.cat(ys)
        # print(x.shape, y.shape)
        f = torch.cat(fs)
        x = x.reshape(-1, M.n_pts1 * M.dim_loi)
        x = torch.cat([x, f], 1)
        x = x.detach().cpu().numpy()
        
        # for deducing input shape of fc2
        # print("pooling expected input:", x.shape)
        
        if self.fc2_batch_size != x.shape[0]:
            # print("batch size change")
            self.fc2_batch_size = x.shape[0]
            input_layer = next(iter(self.fc2_unloaded.inputs))
            self.fc2_unloaded.reshape({input_layer: x.shape})
            self.fc2 = self.ie.load_network(network=self.fc2_unloaded, 
                                            device_name='CPU', 
                                            num_requests=1)
        
        start2 = time.time()
        x = self.fc2.infer({
            next(iter(self.fc2.inputs)): x
        })[next(iter(self.fc2.outputs))]
        x = torch.from_numpy(x).float().to(self.device_used).flatten()
        stamp2 = time.time() - start2
        
        p = torch.cat(ps)
        s = torch.sigmoid(x)
        b = s > 0.5
        lines = []
        score = []
        
        start3 = time.time()
        for i in range(n_batch):
            p0 = p[idx[i] : idx[i + 1]]
            s0 = s[idx[i] : idx[i + 1]]
            mask = b[idx[i] : idx[i + 1]]
            p0 = p0[mask]
            s0 = s0[mask]
            if len(p0) == 0:
                lines.append(torch.zeros([1, M.n_out_line, 2, 2], device=p.device))
                score.append(torch.zeros([1, M.n_out_line], device=p.device))
            else:
                arg = torch.argsort(s0, descending=True)
                p0, s0 = p0[arg], s0[arg]
                lines.append(p0[None, torch.arange(M.n_out_line) % len(p0)])
                score.append(s0[None, torch.arange(M.n_out_line) % len(s0)])
            for j in range(len(jcs[i])):
                if len(jcs[i][j]) == 0:
                    jcs[i][j] = torch.zeros([M.n_out_junc, 2], device=p.device)
                jcs[i][j] = jcs[i][j][
                    None, torch.arange(M.n_out_junc) % len(jcs[i][j])
                ]
        stamp3 = time.time() - start3
        
        result["preds"]["lines"] = torch.cat(lines).detach().cpu().numpy()
        result["preds"]["score"] = torch.cat(score).detach().cpu().numpy()
        result["preds"]["juncs"] = torch.cat([jcs[i][0] for i in range(n_batch)]) \
                                        .detach().cpu().numpy()
        if len(jcs[i]) > 1:
            result["preds"]["junts"] = torch.cat(
                [jcs[i][1] for i in range(n_batch)]
            ).detach().cpu().numpy()

        result["preds"]['jmap'] = result["preds"]['jmap'].detach().cpu().numpy()
        result['preds']['joff'] = result['preds']['joff'].detach().cpu().numpy()
        
        def trunc(values, decs=0):
            return np.trunc(values*10**decs)/(10**decs)
        
        return result, str(trunc(np.array([stamp1, stamp2, stamp3]), decs=4))

    def sample_lines(self, meta, jmap, joff, mode):
        with torch.no_grad():
            junc = meta["junc"]  # [N, 2]
            jtyp = meta["jtyp"]  # [N]
            Lpos = meta["Lpos"]
            Lneg = meta["Lneg"]

            n_type = jmap.shape[0]
            jmap = non_maximum_suppression(jmap).reshape(n_type, -1)
            joff = joff.reshape(n_type, 2, -1)
            max_K = M.n_dyn_junc // n_type
            N = len(junc)
            if mode != "training":
                K = min(int((jmap > M.eval_junc_thres).float().sum().item()), max_K)
            else:
                K = min(int(N * 2 + 2), max_K)
            if K < 2:
                K = 2
                
            # print("K:", K)
            # print("N:", N)
            device = jmap.device

            # index: [N_TYPE, K]
            score, index = torch.topk(jmap, k=K)
            y = (index / 128).float() + torch.gather(joff[:, 0], 1, index) + 0.5
            x = (index % 128).float() + torch.gather(joff[:, 1], 1, index) + 0.5

            # xy: [N_TYPE, K, 2]
            xy = torch.cat([y[..., None], x[..., None]], dim=-1)
            xy_ = xy[..., None, :]
            del x, y, index

            # dist: [N_TYPE, K, N]
            dist = torch.sum((xy_ - junc) ** 2, -1)
            cost, match = torch.min(dist, -1)

            # xy: [N_TYPE * K, 2]
            # match: [N_TYPE, K]
            # print(match.shape)
            # print(cost.shape)
            # print(jtyp.shape)
            # print("n_type", n_type)
            for t in range(n_type):
                # print(t)
                # print(match[t, :])
                match[t, jtyp[match[t]] != t] = N
            # print(cost > 1.5 * 1.5)
            match[cost > 1.5 * 1.5] = N
            match = match.flatten()

            _ = torch.arange(n_type * K, device=device)
            u, v = torch.meshgrid(_, _)
            u, v = u.flatten(), v.flatten()
            # print(match, u, v)
            up, vp = match[u], match[v]
            # print(up, vp)
            label = Lpos[up, vp]
            # print("sample line label",Lpos.shape,up.shape,vp.shape,label.shape)

            c = (u < v).flatten()

            # sample lines
            u, v, label = u[c], v[c], label[c]
            xy = xy.reshape(n_type * K, 2)
            xyu, xyv = xy[u], xy[v]

            u2v = xyu - xyv
            u2v /= torch.sqrt((u2v ** 2).sum(-1, keepdim=True)).clamp(min=1e-6)
            feat = torch.cat(
                [
                    xyu / 128 * M.use_cood,
                    xyv / 128 * M.use_cood,
                    u2v * M.use_slop,
                    (u[:, None] > K).float(),
                    (v[:, None] > K).float(),
                ],
                1,
            )
            #  print(M.use_cood, M.use_slop)
            line = torch.cat([xyu[:, None], xyv[:, None]], 1)
            # print(line.shape)

            xy = xy.reshape(n_type, K, 2)
            # print(xy.shape)
            # print(n_type)
            jcs = [xy[i, score[i] > 0.03] for i in range(n_type)]
            # print(jcs)
            # print(xy[0, :, :])
            # print(np.array(jcs).shape)
            # print(label, label.shape)
            return line, label.float(), feat, jcs


def non_maximum_suppression(a):
    ap = F.max_pool2d(a, 3, stride=1, padding=1)
    mask = (a == ap).float().clamp(min=0.0)
    return a * mask

