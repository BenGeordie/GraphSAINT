import torch
from torch import nn
import scipy.sparse as sp


F_ACT = {'relu': nn.ReLU(),
         'I': lambda x:x}

BN_MANUAL = False
"""
NOTE
    For the various GNN layers, we optionally support batch normalization. Yet, due to the 
    non-IID nature of GNN samplers (whether it is graph-sampling or layer sampling based),
    we may need some modification to the standard batch-norm layer operations to achieve 
    optimal accuracy on graphs. 

    The study of optimal GNN-based batch-norm is out-of-scope for the current version of 
    GraphSAINT. So as a compromise, we provide multiple implementations of batch-norm
    layer which can be optionally inserted at the output of the GNN layers. 

Specifically, we have the various choices for the <bias> field in the layer classes
    'bias'          means no batch-norm is applied. Only add the bias to the hidden
                    features of the GNN layer.
    'norm'          means we calculate the mean and variance of the GNN hidden features,
                    and then scale the hidden features manually by the mean and variance.
                    In this case, we need explicitly create the 'offset' and 'scale' params.
    'norm-nn'       means we use the torch.nn.BatchNorm1d layer implemented by torch. 
                    In this case, no need to explicitly maintain the BN internal params. 
"""


class HighOrderAggregator(nn.Module):
    def __init__(self, dim_in, dim_out, dropout=0., act='relu', \
            order=1, aggr='mean', bias='norm-nn', **kwargs):
        super(HighOrderAggregator,self).__init__()
        assert bias in ['bias', 'norm', 'norm-nn']
        self.order,self.aggr = order,aggr
        self.act, self.bias = F_ACT[act], bias
        self.dropout = dropout
        self.f_lin, self.f_bias = [], []
        self.offset, self.scale = [], []
        for o in range(self.order+1):
            self.f_lin.append(nn.Linear(dim_in,dim_out,bias=False))
            nn.init.xavier_uniform_(self.f_lin[-1].weight)
            self.f_bias.append(nn.Parameter(torch.zeros(dim_out)))
            self.offset.append(nn.Parameter(torch.zeros(dim_out)))
            self.scale.append(nn.Parameter(torch.ones(dim_out)))
        self.f_lin = nn.ModuleList(self.f_lin)
        self.f_dropout = nn.Dropout(p=self.dropout)
        self.params = nn.ParameterList(self.f_bias+self.offset+self.scale)
        self.f_bias = self.params[:self.order+1]
        if self.bias == 'norm':
            self.offset = self.params[self.order+1:2*self.order+2]
            self.scale = self.params[2*self.order+2:]
        elif self.bias == 'norm-nn':
            final_dim_out = dim_out*((aggr=='concat')*(order+1)+(aggr=='mean'))
            self.f_norm = nn.BatchNorm1d(final_dim_out,eps=1e-9,track_running_stats=True)

    def _spmm(self, adj_norm, _feat):
        # alternative ways: use geometric.propagate or torch.mm
        return torch.sparse.mm(adj_norm, _feat)

    def _f_feat_trans(self, _feat, _id):
        feat = self.act(self.f_lin[_id](_feat) + self.f_bias[_id])
        if self.bias == 'norm':
            mean = feat.mean(dim=1).view(feat.shape[0],1)
            var = feat.var(dim=1, unbiased=False).view(feat.shape[0],1)+1e-9
            feat_out = (feat - mean) * self.scale[_id] * torch.rsqrt(var) + self.offset[_id]
        else:
            feat_out = feat
        return feat_out

    def forward(self, inputs):
        """
        Inputs:.
            adj_norm        edge-list represented adj matrix
        """
        adj_norm, feat_in = inputs
        feat_in = self.f_dropout(feat_in)
        feat_hop = [feat_in]
        # generate A^i X
        for o in range(self.order):
            # propagate(edge_index, x=x, norm=norm)
            feat_hop.append(self._spmm(adj_norm, feat_hop[-1]))
        feat_partial = [self._f_feat_trans(ft,idf) for idf,ft in enumerate(feat_hop)]
        if self.aggr == 'mean':
            feat_out = feat_partial[0]
            for o in range(len(feat_partial)-1):
                feat_out += feat_partial[o+1]
        elif self.aggr == 'concat':
            feat_out = torch.cat(feat_partial,1)
        else:
            raise NotImplementedError
        if self.bias == 'norm-nn':
            feat_out = self.f_norm(feat_out)
        return adj_norm, feat_out       # return adj_norm to support Sequential


class JumpingKnowledge(nn.Module):
    def __init__(self):
        """
        To be added soon
        """
        pass


class AttentionAggregator(nn.Module):
    '''
    Attention mechanism by GAT. We remove the softmax step since during minibatch training, we cannot see all neighbors of a node.
    For order>1, obtain attention from neighbor features from lower order features.
    '''
    def __init__(self, dim_in, dim_out, dropout=0., act='relu', \
            order=1, aggr='mean', bias='norm', mulhead=1):
        super(AttentionAggregator,self).__init__()
        assert bias in ['bias', 'norm', 'norm-nn']
        self.mulhead=mulhead
        self.order,self.aggr = order,aggr
        self.act, self.bias = F_ACT[act], bias
        self.att_act=nn.LeakyReLU(negative_slope=0.2)
        self.dropout = dropout
        self._f_lin = []
        self._offset, self._scale = [], []
        self._attention = []
        # mostly we have order = 1 for GAT
        # "+1" since we do batch norm of order-0 and order-1 outputs separately
        for o in range(self.order+1):
            for i in range(self.mulhead):
                self._f_lin.append(nn.Linear(dim_in, int(dim_out/self.mulhead), bias=True))
                nn.init.xavier_uniform_(self._f_lin[-1].weight)
                # _offset and _scale are for 'norm' type of batch norm
                self._offset.append(nn.Parameter(torch.zeros(int(dim_out/self.mulhead))))
                self._scale.append(nn.Parameter(torch.ones(int(dim_out/self.mulhead))))
                if o < self.order:
                    self._attention.append(nn.Parameter(torch.ones(1, int(dim_out/self.mulhead*2))))
                    nn.init.xavier_uniform_(self._attention[-1])
        self.mods = nn.ModuleList(self._f_lin)
        self.f_dropout = nn.Dropout(p=self.dropout)
        self.params = nn.ParameterList(self._offset + self._scale + self._attention)
        self.f_lin = []
        self.offset, self.scale = [], []
        self.attention = []
        # Reshaping the params: we need to explicitly reshape this since nn.ParameterList cannot cannot
        # convert nested list of nn parameters. 
        for o in range(self.order+1):
            self.f_lin.append([])
            self.offset.append([])
            self.scale.append([])
            self.attention.append([])
            for i in range(self.mulhead):
                self.f_lin[-1].append(self.mods[o*self.mulhead + i])
                if self.bias == 'norm':     # not used in 'norm-nn' mode
                    self.offset[-1].append(self.params[o*self.mulhead + i])
                    self.scale[-1].append(self.params[len(self._offset) + o*self.mulhead + i])
                if o < self.order:      # excluding the order-0 part
                    self.attention[-1].append(self.params[len(self._offset)*2 + o*self.mulhead + i])
        if self.bias == 'norm-nn':
            final_dim_out = dim_out*((aggr=='concat')*(order+1) + (aggr=='mean'))
            self.f_norm = nn.BatchNorm1d(final_dim_out, eps=1e-9, track_running_stats=True)

    def _spmm(self, adj_norm, _feat):
        return torch.sparse.mm(adj_norm, _feat)

    def _f_feat_trans(self, _feat, f_lin):
        feat_out = []
        for i in range(self.mulhead):
            feat_out.append(self.act(f_lin[i](_feat)))
        return feat_out

    def _aggregate_attention(self, adj, feat_neigh, feat_self, attention):
        attention_self = self.att_act(attention[:, :feat_self.shape[1]].mm(feat_self.t())).squeeze()
        attention_neigh = self.att_act(attention[:, feat_neigh.shape[1]:].mm(feat_neigh.t())).squeeze()
        attention_norm = (attention_self[adj._indices()[0]] + attention_neigh[adj._indices()[1]]) * adj._values()
        att_adj = torch.sparse.FloatTensor(adj._indices(), attention_norm, torch.Size(adj.shape))
        return self._spmm(att_adj, feat_neigh)

    def forward(self, inputs):
        """
        Inputs:.
            adj_norm        edge-list represented adj matrix
        """
        adj_norm, feat_in = inputs
        feat_in = self.f_dropout(feat_in)
        # generate A^i X
        feat_partial=list()
        for o in range(self.order+1):
            feat_partial.append(self._f_feat_trans(feat_in, self.f_lin[o]))
        for o in range(1,self.order+1):
            for s in range(o):
                for i in range(self.mulhead):
                    feat_partial[o][i] = self._aggregate_attention(adj_norm,feat_partial[o][i],feat_partial[o-s-1][i],self.attention[o-1][i])
        if self.bias == 'norm':
            # normalize per-order, per-head
            for o in range(self.order + 1):
                for i in range(self.mulhead):
                    mean = feat_partial[o][i].mean(dim=1).unsqueeze(1)
                    var = feat_partial[o][i].var(dim=1, unbiased=False).unsqueeze(1) + 1e-9
                    feat_partial[o][i] = (feat_partial[o][i] - mean) * self.scale[o][i] * torch.rsqrt(var) + self.offset[o][i]
 
        for o in range(self.order + 1):
            feat_partial[o] = torch.cat(feat_partial[o], 1)
        if self.aggr == 'mean':
            feat_out = feat_partial[0]
            for o in range(len(feat_partial) - 1):
                feat_out += feat_partial[o + 1]
        elif self.aggr == 'concat':
            feat_out = torch.cat(feat_partial, 1)
        else:
            raise NotImplementedError
        if self.bias == 'norm-nn':
            feat_out = self.f_norm(feat_out)
        return adj_norm, feat_out 


class GatedAttentionAggregator(nn.Module):
    '''
    Attention mechanism by GAAN. Only support order<=1.
    dim_gate is the output dimension of theta_m for the gate generation network.
    '''
    def __init__(self, dim_in, dim_out, dropout=0., act='relu', \
            order=1, aggr='mean', bias='norm', mulhead=1, dim_gate=64):
        super(GatedAttentionAggregator,self).__init__()
        self.mulhead=mulhead
        self.order,self.aggr = order,aggr
        self.act, self.bias = F_ACT[act], bias
        self.att_act=nn.LeakyReLU(negative_slope=0.2)
        self.dropout = dropout
        self.dim_gate=dim_gate
        self._f_lin=list()
        self._offset=list()
        self._scale=list()
        self._attention=list()
        for o in range(self.order+1):
            self._offset.append(nn.Parameter(torch.zeros(dim_out)))
            self._scale.append(nn.Parameter(torch.ones(dim_out)))
            for i in range(self.mulhead):
                self._f_lin.append(nn.Linear(dim_in,int(dim_out/self.mulhead),bias=True))
                nn.init.xavier_uniform_(self._f_lin[-1].weight)
                if o<self.order:
                    self._attention.append(nn.Parameter(torch.ones(1,int(dim_out/self.mulhead*2))))
                    nn.init.xavier_uniform_(self._attention[-1])
        self._weight_gate=nn.Parameter(torch.ones(dim_in*2+dim_gate,self.mulhead))
        nn.init.xavier_uniform_(self._weight_gate)
        self._weight_pool_gate=nn.Parameter(torch.ones(dim_in,dim_gate))
        nn.init.xavier_uniform_(self._weight_pool_gate)
        self.mods = nn.ModuleList(self._f_lin)
        self.f_dropout = nn.Dropout(p=self.dropout)
        self.params=nn.ParameterList(self._offset+self._scale+self._attention+[self._weight_gate,self._weight_pool_gate])
        self.f_lin=list()
        self.offset=list()
        self.scale=list()
        self.attention=list()
        for o in range(self.order+1):
            self.f_lin.append(list())
            self.attention.append(list())
            self.offset.append(self.params[o])
            self.scale.append(self.params[len(self._offset)+o])
            for i in range(self.mulhead):
                self.f_lin[-1].append(self.mods[o*self.mulhead+i])
                if o<self.order:
                    self.attention[-1].append(self.params[len(self._offset)*2+o*self.mulhead+i])
        self.weight_gate=self.params[-2]
        self.weight_pool_gate=self.params[-1]

    def _spmm(self, adj_norm, _feat):
        return torch.sparse.mm(adj_norm, _feat)

    def _f_feat_trans(self, _feat, f_lin):
        feat_out=list()
        for i in range(self.mulhead):
            feat_out.append(self.act(f_lin[i](_feat)))
            # if self.bias=='norm':
            #     mean=feat_out[-1].mean(dim=1).view(feat.shape[0],1)
            #     var=feat_out[-1].var(dim=1,unbiased=False).view(feat.shape[0],1)+1e-9
            #     feat_out[-1]=(feat_out[-1]-mean)*scale[i]*torch.rsqrt(var)+offset[i]
        return feat_out

    def _aggregate_attention(self,adj,feat_neigh,feat_self,attention):
        attention_self=self.att_act(attention[:,:feat_self.shape[1]].mm(feat_self.t())).squeeze()
        attention_neigh=self.att_act(attention[:,feat_neigh.shape[1]:].mm(feat_neigh.t())).squeeze()
        att_adj=torch.sparse.FloatTensor(adj._indices(),(attention_self[adj._indices()[0]]+attention_neigh[adj._indices()[1]])*adj._values(),torch.Size(adj.shape))
        return self._spmm(att_adj,feat_neigh)

    def _batch_norm(self,feat):
        for o in range(self.order+1):
            mean=feat[o].mean(dim=1).unsqueeze(1)
            var=feat[o].var(dim=1,unbiased=False).unsqueeze(1)+1e-9
            feat[o]=(feat[o]-mean)*self.scale[o]*torch.rsqrt(var)+self.offset[o]
        return feat

    def _compute_gate_value(self,adj,feat,adj_sp_csr):
        """
        See equation (3) of GaAN: Gated Attention Networks for Learning on Large and Spatiotemporal Graphs
        """
        zj=feat.mm(self.weight_pool_gate)
        neigh_zj=list()
        # use loop instead since torch does not support sparse tensor slice
        for i in range(adj.shape[0]):
            if adj_sp_csr.indptr[i]<adj_sp_csr.indptr[i+1]:
                neigh_zj.append(torch.max(zj[adj_sp_csr.indices[adj_sp_csr.indptr[i]:adj_sp_csr.indptr[i+1]]],0)[0].unsqueeze(0))
            else:
                if zj.is_cuda:
                    neigh_zj.append(torch.zeros(1,self.dim_gate).cuda())
                else:
                    neigh_zj.append(torch.zeros(1,self.dim_gate))
        neigh_zj=torch.cat(neigh_zj,0)
        neigh_mean=self._spmm(adj,feat)
        gate_feat=torch.cat([feat,neigh_zj,neigh_mean],1)
        return gate_feat.mm(self.weight_gate)



    def forward(self, inputs):
        """
        Inputs:.
            adj_norm        edge-list represented adj matrix
        """
        adj_norm, feat_in = inputs
        feat_in = self.f_dropout(feat_in)
        # compute gate value
        adj_norm_cpu=adj_norm.cpu()
        adj_norm_sp_csr=sp.coo_matrix((adj_norm_cpu._values().numpy(),(adj_norm_cpu._indices()[0].numpy(),adj_norm_cpu._indices()[1].numpy())),shape=(adj_norm.shape[0],adj_norm.shape[0])).tocsr()
        gate_value=self._compute_gate_value(adj_norm,feat_in,adj_norm_sp_csr)
        feat_partial=list()
        for o in range(self.order+1):
            feat_partial.append(self._f_feat_trans(feat_in,self.f_lin[o]))
        for o in range(1,self.order+1):
            for s in range(o):
                for i in range(self.mulhead):
                    feat_partial[o][i]=self._aggregate_attention(adj_norm,feat_partial[o][i],feat_partial[o-s-1][i],self.attention[o-1][i])
                    feat_partial[o][i]*=gate_value[:,i].unsqueeze(1)
        for o in range(self.order+1):
            feat_partial[o]=torch.cat(feat_partial[o],1)
        # if norm before concatnation, gate value vanishes
        if self.bias == 'norm':
            feat_partial = self._batch_norm(feat_partial)
        if self.aggr == 'mean':
            feat_out = feat_partial[0]
            for o in range(len(feat_partial)-1):
                feat_out += feat_partial[o+1]
        elif self.aggr == 'concat':
            feat_out = torch.cat(feat_partial,1)
        else:
            raise NotImplementedError
        return adj_norm, feat_out 
