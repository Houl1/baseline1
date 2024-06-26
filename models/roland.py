# coding: utf-8
import math
import torch
from torch.nn.parameter import Parameter
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GCNConv
import deepsnap
from typing import Optional

import torch
import torch.nn as nn
from torch.nn import Parameter
from torch_scatter import scatter_add
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import add_remaining_self_loops

from torch_geometric.nn.inits import glorot, zeros
# from graphgym.config import cfg

import pdb



class Roland(torch.nn.Module):
    input_dim: int
    hidden_dim: int
    output_dim: int
    method_name: str
    egcn_type: str

    def __init__(self,args, input_dim, hidden_dim, output_dim):
        super().__init__()
        self.args = args
        if args.tasktype == "multisteps":
            self.num_time_steps = args.time_steps-5
        else:
            self.num_time_steps = args.time_steps-1
        # if args.multisteps_pre == "True":   
        #     self.num_time_steps = args.time_steps-5
        # else:
        #     self.num_time_steps = args.time_steps-1
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.output_dim = output_dim
        
        self.GenralGNN1 = GeneralConvLayer(1,hidden_dim)
        self.GenralGNN2 = GeneralConvLayer(hidden_dim,output_dim)
        # self.GenralGNN1 = GCNConv(input_dim, hidden_dim, dropout=0.1)
        # self.GenralGNN2 = GCNConv(hidden_dim, output_dim, dropout=0.1)
        self.GRU_layer1 = GRUUpdater(hidden_dim, hidden_dim)
        self.GRU_layer2 = GRUUpdater(hidden_dim,hidden_dim)
        # self.GRU_layer1 = GraphConvGRUUpdater(hidden_dim, hidden_dim)
        # self.GRU_layer2 = GraphConvGRUUpdater(hidden_dim,hidden_dim)

        # self.lin = nn.Linear(output_dim, input_dim, bias=False)

    def forward(self, graphs):
        
        h1_list=list()
        h2_list=list()
        for i in range(self.num_time_steps):
            edge_index = graphs[i].edge_index
            edge_weight = graphs[i].edge_weight
            feat = graphs[i].x
            h1 = self.GenralGNN1(feat, edge_index,edge_weight)
            
            if i==0:
                gru1 = self.GRU_layer1(h1,h1)
                # gru1 = self.GRU_layer1(h1,h1,edge_index,feat)
            else:
                gru1 = self.GRU_layer1(h1,h1_list[i-1])
            h2 = self.GenralGNN2(gru1, edge_index,edge_weight)
            h1_list.append(gru1)
            if i==0:
                gru2 = self.GRU_layer2(h2,h2)
            else:
                gru2 = self.GRU_layer2(h2,h2_list[i-1])
            h2_list.append(gru2)
        return h2_list
    
    def get_loss(self, feed_dict):
        # graphs = feed_dict["graphs"]
        # run gnn
        # graphs = graphs[:-1]
        # gnd = torch_geometric.utils.to_scipy_sparse_matrix(graphs[-1].edge_index).todense() 
        # final_emb = self.forward(graphs)
        node_1, node_2, node_2_negative, graphs = feed_dict.values()
        bceloss = nn.BCELoss()
        final_emb = self.forward(graphs)
        self.graph_loss = 0
        for t in range(self.num_time_steps):
            emb_t = final_emb[t]
            source_node_emb = emb_t[node_1[t]]
            tart_node_pos_emb = emb_t[node_2[t]]
            tart_node_neg_emb = emb_t[node_2_negative[t]]
            pos_bceloss = nn.BCELoss()
            neg_bceloss = nn.BCELoss()
                                  
            pos_score = torch.sum(source_node_emb*tart_node_pos_emb, dim=1)
            neg_score = -torch.sum(source_node_emb[ :,None , :]*tart_node_neg_emb, dim=2).flatten()
            
                        
            pos_score = torch.sigmoid(pos_score)
            neg_score = torch.sigmoid(neg_score)

            
            pos_loss = pos_bceloss(pos_score, torch.ones_like(pos_score))
            neg_loss = neg_bceloss(neg_score, torch.ones_like(neg_score))

            loss = pos_loss + self.args.neg_weight * neg_loss
            self.graph_loss += loss
        return self.graph_loss  

class GRU_cell(nn.Module):
    def __init__(self, feature_size, hidden_size):
        super(GRU_cell, self).__init__()
        self.hidden_size = hidden_size  # 隐层大小
        self.num_layers = 1  # gru层数
        # feature_size为特征维度，就是每个时间点对应的特征数量，这里为1
        self.gru = nn.GRU(feature_size, hidden_size, self.num_layers, batch_first=True)
        self.fc = nn.Linear(hidden_size, hidden_size)

    def forward(self, x, hidden=None):
        batch_size = x.shape[0] # 获取批次大小
        
        # 初始化隐层状态
        if hidden is None:
            h_0 = x.data.new(self.num_layers, batch_size, self.hidden_size).fill_(0).float()
            # print(x.shape)
            # h_0 = x 
        else:
            h_0 = hidden
            
        # GRU运算
        output, h_0 = self.gru(x, h_0)
        
        # 获取GRU输出的维度信息
        # batch_size, timestep, hidden_size = output.shape  
            
        # 将output变成 batch_size * timestep, hidden_dim
        # output = output.reshape(-1, hidden_size)
        
        # 全连接层
        # output = self.fc(output)  # 形状为batch_size * timestep, 1
        
        # 转换维度，用于输出
        # output = output.reshape(timestep, batch_size, -1)
        # output = output.transpose(0,1)
        
        # 我们只需要返回最后一个时间片的数据即可
        return output

class GRUUpdater(nn.Module):
    """
    Node embedding update block using standard GRU and variations of it.
    """
    def __init__(self, dim_in, dim_out):
        # dim_in (dim of X): dimension of input node_feature.
        # dim_out (dim of H): dimension of previous and current hidden states.
        # forward(X, H) --> H.
        super(GRUUpdater, self).__init__()
        # self.layer_id = layer_id
        self.GRU_Z = nn.Sequential(
            nn.Linear(dim_in + dim_out, dim_out, bias=True),
            nn.Sigmoid())
        # reset gate.
        self.GRU_R = nn.Sequential(
            nn.Linear(dim_in + dim_out, dim_out, bias=True),
            nn.Sigmoid())
        # new embedding gate.
        self.GRU_H_Tilde = nn.Sequential(
            nn.Linear(dim_in + dim_out, dim_out, bias=True),
            nn.Tanh())
    
    def forward(self, X, H_prev):
        # H_prev = batch.node_states[self.layer_id]
        # X = batch.node_feature
        Z = self.GRU_Z(torch.cat([X, H_prev], dim=1))
        R = self.GRU_R(torch.cat([X, H_prev], dim=1))
        H_tilde = self.GRU_H_Tilde(torch.cat([X, R * H_prev], dim=1))
        H_gru = Z * H_prev + (1 - Z) * H_tilde

        # if cfg.gnn.embed_update_method == 'masked_gru':
        #     # Update for active nodes only, use output from GRU.
        #     keep_mask = (batch.node_degree_new == 0)
        #     H_out = H_gru
        #     # Reset inactive nodes' embedding.
        #     H_out[keep_mask, :] = H_prev[keep_mask, :]
        # elif cfg.gnn.embed_update_method == 'moving_average_gru':
        #     # Only update for active nodes, using moving average with output from GRU.
        #     H_out = H_prev * batch.keep_ratio + H_gru * (1 - batch.keep_ratio)
        # elif cfg.gnn.embed_update_method == 'gru':
        #     # Update all nodes' embedding using output from GRU.
        #     H_out = H_gru
        return H_gru

class GraphConvGRUUpdater(nn.Module):
    """
    Node embedding update block using GRU with internal GNN and variations of
    it.
    """
    def __init__(self, dim_in, dim_out):
        # dim_in (dim of X): dimension of input node_feature.
        # dim_out (dim of H): dimension of previous and current hidden states.
        # forward(X, H) --> H.
        super(GraphConvGRUUpdater, self).__init__()
        # self.layer_id = layer_id
        
        self.GRU_Z = GeneralConvLayer(dim_in + dim_out, dim_out)
        # reset gate.
        self.GRU_R = GeneralConvLayer(dim_in + dim_out, dim_out)
        # new embedding gate.
        self.GRU_H_Tilde = GeneralConvLayer(dim_in + dim_out, dim_out)

    def forward(self, X, H_prev,edge_index,feat):
        # H_prev = batch.node_states[self.layer_id]
        # X = batch.node_feature
        # Combe previous node embedding and current feature for message passing.
        batch_z = deepsnap.graph.Graph()
        batch_z.node_feature = torch.cat([X, H_prev], dim=1).clone()
        batch_z.edge_feature = feat.clone()
        batch_z.edge_index = edge_index.clone()

        batch_r = deepsnap.graph.Graph()
        batch_r.node_feature = torch.cat([X, H_prev], dim=1).clone()
        batch_r.edge_feature = feat.clone()
        batch_r.edge_index = edge_index.clone()

        # (num_nodes, dim_out)
        Z = nn.functional.sigmoid(self.GRU_Z(batch_z).node_feature)
        # (num_nodes, dim_out)
        R = nn.functional.sigmoid(self.GRU_R(batch_r).node_feature)

        batch_h = deepsnap.graph.Graph()
        batch_h.node_feature = torch.cat([X, R * H_prev], dim=1).clone()
        batch_h.edge_feature = feat.clone()
        batch_h.edge_index = edge_index.clone()

        # (num_nodes, dim_out)
        H_tilde = nn.functional.tanh(self.GRU_H_Tilde(batch_h).node_feature)
        H_gru = Z * H_prev + (1 - Z) * H_tilde

        # if cfg.gnn.embed_update_method == 'masked_gru':
        #     # Update for active nodes only, use output from GRU.
        #     keep_mask = (batch.node_degree_new == 0)
        #     H_out = H_gru
        #     # Reset inactive nodes' embedding.
        #     H_out[keep_mask, :] = H_prev[keep_mask, :]
        # elif cfg.gnn.embed_update_method == 'moving_average_gru':
        #     # Only update for active nodes, using moving average with output from GRU.
        #     H_out = H_prev * batch.keep_ratio + H_gru * (1 - batch.keep_ratio)
        # elif cfg.gnn.embed_update_method == 'gru':
        #     # Update all nodes' embedding using output from GRU.
        #     H_out = H_gru

        return H_gru


class GeneralConvLayer(MessagePassing):
    r"""General graph convolution layer.
    """

    def __init__(self, in_channels, out_channels, improved=False, cached=False,
                 bias=True):
        """
        Args:
            in_channels: dimension of input node features.
            out_channels: dimension of output node embeddings.
            improved:
            cached:
            bias:
            **kwargs:
        """
        super(GeneralConvLayer, self).__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.improved = improved
        self.cached = cached
        self.normalize = 0

        self.weight = Parameter(torch.Tensor(in_channels, out_channels))

        if bias:
            self.bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters()

    def reset_parameters(self):
        glorot(self.weight)
        zeros(self.bias)
        self.cached_result = None
        self.cached_num_edges = None

    @staticmethod
    def norm(edge_index, num_nodes, edge_weight=None, improved=False,
             dtype=None):
        r"""

        Args:
            edge_index: shape [2, num_edges]
            num_nodes:
            edge_weight:
            improved:
            dtype:

        Returns:

        """
        if edge_weight is None:
            # The unweighted case, edge_weight = 1.
            edge_weight = torch.ones((edge_index.size(1),), dtype=dtype,
                                     device=edge_index.device)
        # Add self-loops for nodes v such that (v, v) not in E, self-loops have weights 1 or 2.
        fill_value = 1 if not improved else 2
        edge_index, edge_weight = add_remaining_self_loops(
            edge_index, edge_weight, fill_value, num_nodes)

        row, col = edge_index  # source node indices, destination node indices.
        # deg[v] = sum(edge_weight[i] for i in {0,1,...,num_nodes-1} s.t. row[i] == v)
        deg = scatter_add(edge_weight, row, dim=0, dim_size=num_nodes)
        deg_inv_sqrt = deg.pow(-0.5)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
        # normalize weight weight, w[u, v] = w[u, v] / sqrt(deg(u) * deg(v)).
        return edge_index, deg_inv_sqrt[row] * edge_weight * deg_inv_sqrt[col]

    def forward(self, x, edge_index, edge_weight=None, edge_feature=None):
        # Note: bias, if requested, will be applied after message aggregation.
        x = torch.matmul(x, self.weight)

        # If caching is requested and there exists previous cached edge_index.
        if self.cached and self.cached_result is not None:
            if edge_index.size(1) != self.cached_num_edges:
                raise RuntimeError(
                    'Cached {} number of edges, but found {}. Please '
                    'disable the caching behavior of this layer by removing '
                    'the `cached=True` argument in its constructor.'.format(
                        self.cached_num_edges, edge_index.size(1)))

        # If caching is not requested or we need to initialize cache.
        if not self.cached or self.cached_result is None:
            self.cached_num_edges = edge_index.size(1)
            if self.normalize:
                edge_index, norm = self.norm(edge_index, x.size(self.node_dim),
                                             edge_weight, self.improved,
                                             x.dtype)
            else:
                # Use the un-normalized edge weight.
                norm = edge_weight
            # Save (initialize) edge_index and normalized edge weights to cache.
            self.cached_result = edge_index, norm

        # Load from current cache.
        edge_index, norm = self.cached_result

        return self.propagate(edge_index, x=x, norm=norm,
                              edge_feature=edge_feature)

    def message(
        self,
        x_j: torch.Tensor,
        norm: Optional[torch.Tensor],
        edge_feature: Optional[torch.Tensor]
    ) -> torch.Tensor:
        r"""
        Args:
            x_j: shape [num_edges, num_node_features]
            norm: shape [num_edges]
            edge_feature: [num_edges, num_edge_features]

        Returns:

        """
        if edge_feature is None:
            # If no additional edge features are provided, the message is simply
            # the weighted features of the source node j.
            return norm.view(-1, 1) * x_j if norm is not None else x_j
        else:
            # If there are edge features, add to node features before applying edge weight.
            return norm.view(-1, 1) * (
                    x_j + edge_feature) if norm is not None else (
                    x_j + edge_feature)

    def update(self, aggr_out):
        if self.bias is not None:
            aggr_out = aggr_out + self.bias
        return aggr_out

    def __repr__(self):
        return '{}({}, {})'.format(self.__class__.__name__, self.in_channels,
                                   self.out_channels)


