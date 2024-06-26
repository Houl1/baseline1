# -*- encoding: utf-8 -*-
'''
@File    :   train.py
@Time    :   2021/02/20 10:25:13
@Author  :   Fei gao 
@Contact :   feig@mail.bnu.edu.cn
BNU, Beijing, China
'''
import argparse
import networkx as nx
import numpy as np
import dill
import pickle as pkl
import scipy
from torch.utils.data import DataLoader
import scipy.sparse as sp
from utils.preprocess import load_graphs, get_context_pairs, get_evaluation_data
from utils.minibatch import  MyDataset
from utils.utilities import to_device,get_activity_from_emb
from eval.link_prediction import evaluate_classifier
from eval.node_activity_prediction import get_score_t
from models.model import DySAT

import torch
import wandb
import random
import os
import torch.backends.cudnn as cudnn
wandb.init(project="dysat", entity="houjinlin")
torch.autograd.set_detect_anomaly(True)

def inductive_graph(graph_former, graph_later):
    """Create the adj_train so that it includes nodes from (t+1) 
       but only edges from t: this is for the purpose of inductive testing.

    Args:
        graph_former ([type]): [description]
        graph_later ([type]): [description]
    """
    newG = nx.MultiGraph()
    newG.add_nodes_from(graph_later.nodes(data=True))
    newG.add_edges_from(graph_former.edges(data=False))
    return newG


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--time_steps', type=int, nargs='?', default=16,
                        help="total time steps used for train, eval and test")
    # Experimental settings.
    parser.add_argument('--dataset', type=str, nargs='?', default='Enron',
                        help='dataset name')
    parser.add_argument('--GPU_ID', type=int, nargs='?', default=0,
                        help='GPU_ID (0/1 etc.)')
    parser.add_argument('--epochs', type=int, nargs='?', default=200,
                        help='# epochs')
    parser.add_argument('--val_freq', type=int, nargs='?', default=1,
                        help='Validation frequency (in epochs)')
    parser.add_argument('--test_freq', type=int, nargs='?', default=1,
                        help='Testing frequency (in epochs)')
    parser.add_argument('--batch_size', type=int, nargs='?', default=512,
                        help='Batch size (# nodes)')
    parser.add_argument('--featureless', type=bool, nargs='?', default=True,
                    help='True if one-hot encoding.')
    parser.add_argument("--early_stop", type=int, default=10,
                        help="patient")
    parser.add_argument("--seed", type=int, default=0,
                        help="seed")
    parser.add_argument('--multisteps_pre', type=str, default="False")
    # 1-hot encoding is input as a sparse matrix - hence no scalability issue for large datasets.
    # Tunable hyper-params
    # TODO: Implementation has not been verified, performance may not be good.
    parser.add_argument('--residual', type=bool, nargs='?', default=True,
                        help='Use residual')
    # Number of negative samples per positive pair.
    parser.add_argument('--neg_sample_size', type=int, nargs='?', default=10,
                        help='# negative samples per positive')
    # Walk length for random walk sampling.
    parser.add_argument('--walk_len', type=int, nargs='?', default=20,
                        help='Walk length for random walk sampling')
    # Weight for negative samples in the binary cross-entropy loss function.
    parser.add_argument('--neg_weight', type=float, nargs='?', default=1.0,
                        help='Weightage for negative samples')
    parser.add_argument('--learning_rate', type=float, nargs='?', default=0.01,
                        help='Initial learning rate for self-attention model.')
    parser.add_argument('--spatial_drop', type=float, nargs='?', default=0.1,
                        help='Spatial (structural) attention Dropout (1 - keep probability).')
    parser.add_argument('--temporal_drop', type=float, nargs='?', default=0.5,
                        help='Temporal attention Dropout (1 - keep probability).')
    parser.add_argument('--weight_decay', type=float, nargs='?', default=0.0005,
                        help='Initial learning rate for self-attention model.')
    # Architecture params
    parser.add_argument('--structural_head_config', type=str, nargs='?', default='16,8,8',
                        help='Encoder layer config: # attention heads in each GAT layer')
    parser.add_argument('--structural_layer_config', type=str, nargs='?', default='256',
                        help='Encoder layer config: # units in each GAT layer')
    parser.add_argument('--temporal_head_config', type=str, nargs='?', default='16',
                        help='Encoder layer config: # attention heads in each Temporal layer')
    parser.add_argument('--temporal_layer_config', type=str, nargs='?', default='256',
                        help='Encoder layer config: # units in each Temporal layer')
    parser.add_argument('--position_ffn', type=str, nargs='?', default='True',
                        help='Position wise feedforward')
    parser.add_argument('--window', type=int, nargs='?', default=-1,
                        help='Window for temporal attention (default : -1 => full)')
    parser.add_argument('--tasktype', type=str, default="sigle_step",choices=['siglestep','multisteps','data_scarce'])
    parser.add_argument('--scare_snapshot', type=str, default='')
    args = parser.parse_args()
    print(args)
    wandb.config.update(args)
    
    def setup_seed(seed):
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.cuda.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        cudnn.deterministic = True
    setup_seed(args.seed)
    
    #graphs, feats, adjs = load_graphs(args.dataset)
    graphs, adjs = load_graphs(args.dataset, args.time_steps)
    if args.featureless == True:
        feats = [scipy.sparse.identity(adjs[args.time_steps - 1].shape[0]).tocsr()[range(0, x.shape[0]), :] for x in adjs if
             x.shape[0] <= adjs[args.time_steps - 1].shape[0]]


    assert args.time_steps <= len(adjs), "Time steps is illegal"

    context_pairs_train = get_context_pairs(graphs, adjs)

    # Load evaluation data for link prediction.
    train_edges_pos, train_edges_neg, val_edges_pos, val_edges_neg, \
        test_edges_pos, test_edges_neg = get_evaluation_data(graphs)
    print("No. Train: Pos={}, Neg={} \nNo. Val: Pos={}, Neg={} \nNo. Test: Pos={}, Neg={}".format(
        len(train_edges_pos), len(train_edges_neg), len(val_edges_pos), len(val_edges_neg),
        len(test_edges_pos), len(test_edges_neg)))

    # Create the adj_train so that it includes nodes from (t+1) but only edges from t: this is for the purpose of
    # inductive testing.
    new_G = inductive_graph(graphs[args.time_steps-2], graphs[args.time_steps-1])
    graphs[args.time_steps-1] = new_G
    adjs[args.time_steps-1] = nx.adjacency_matrix(new_G)

    if args.tasktype == "data_scarce":
        scare_data =  list(map(int, args.scare_snapshot.split(",")))
        for i in scare_data:
            control_low = 0
            control_high = args.time_steps-2
            for j in range(i,-1,-1):
                if j not in scare_data:
                    control_low = j
                    break
            for j in range(i,args.time_steps-1):
                if j not in scare_data:
                    control_high = j
                    break
            # print(i)
            # print(control_high)
            # print(control_low)
            low_matrix = sp.coo_matrix(adjs[control_low],dtype=np.float32)
            high_matrix = sp.coo_matrix(adjs[control_high],dtype=np.float32)
            common =  low_matrix +  high_matrix
            mask_simal_2 = np.abs(common.data)<2
            common.data[mask_simal_2]=0
            mask_2to1 = np.abs(common.data)==2
            common.data[mask_2to1]=1
            
            low_no_common = low_matrix-common
            low_no_common = low_matrix.tocoo()
            low_no_common_array = low_no_common.toarray()
              
            ones_indices_low = [(a,b) for a,b in zip(low_no_common.row,low_no_common.col) if low_no_common_array[a][b]==1]
            low_delete_num =int(((control_high-i)/(control_high-control_low))*len(ones_indices_low))
            low_to_delet = random.sample(ones_indices_low,low_delete_num)
            
            for a,b in low_to_delet:
                low_no_common_array[a][b]=0
                        
            high_no_common = high_matrix-common
            high_no_common = high_matrix.tocoo()
            high_no_common_array = high_no_common.toarray()
              
            ones_indices_high = [(a,b) for a,b in zip(high_no_common.row,high_no_common.col) if high_no_common_array[a][b]==1]
            high_delete_num =int(((i-control_low)/(control_high-control_low))*len(ones_indices_high))
            # print(high_delete_num)
            # print(i-control_low)
            high_to_delet = random.sample(ones_indices_high,high_delete_num)
            
            for a,b in high_to_delet:
                high_no_common_array[a][b]=0
            
            adjs[i] = (common+ sp.coo_matrix(low_no_common_array) + sp.coo_matrix(high_no_common_array)).todense()
        for i,adj in enumerate(adjs[:-1]):
            if i not in scare_data:
                adjs[i]=sp.coo_matrix(adj, dtype=np.float32).todense()
    
    
    # build dataloader and model
    device = torch.device('cuda:' + str(args.GPU_ID) if torch.cuda.is_available() else 'cpu')
    dataset = MyDataset(args, graphs, feats, adjs, context_pairs_train)
    dataloader = DataLoader(dataset, 
                            batch_size=args.batch_size, 
                            shuffle=True, 
                            num_workers=0, 
                            collate_fn=MyDataset.collate_fn)
    #dataloader = NodeMinibatchIterator(args, graphs, feats, adjs, context_pairs_train, device) 
    model = DySAT(args, 1, args.time_steps).to(device)
    # model = DySAT(args, 1, args.time_steps).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)

    # in training
    best_epoch_val = 0
    patient = 0
    best_epoch_test = 0
    best_epoch_ap = 0
    best_epoch_mae = 0
    best_epoch_mse = 0
        
    feat = np.array(feats[0].todense())
    rowsum = np.array(feat.sum(1))
    r_inv = np.power(rowsum, -1).flatten()
    r_inv[np.isinf(r_inv)] = 0.
    r_mat_inv = sp.diags(r_inv)
    feat = r_mat_inv.dot(feat)
    # feat = torch.Tensor(feat).to(device)
    
    for epoch in range(args.epochs):
        model.train()
        epoch_loss = []
        for idx, feed_dict in enumerate(dataloader):
            feed_dict = to_device(feed_dict, device)
            opt.zero_grad()
    
            loss = model.get_loss(feed_dict)
            # loss = model.get_loss(feed_dict)
            loss.backward()
            opt.step()
            epoch_loss.append(loss.item())

        model.eval()
        emb = model(feed_dict["graphs"])[:, -2, :].detach().cpu().numpy()
        activity_test = get_activity_from_emb(emb)
        val_results, test_results, _, _,test_ap = evaluate_classifier(train_edges_pos,
                                                            train_edges_neg,
                                                            val_edges_pos, 
                                                            val_edges_neg, 
                                                            test_edges_pos,
                                                            test_edges_neg, 
                                                            emb, 
                                                            emb)
        epoch_auc_val = val_results["HAD"][1]
        epoch_auc_test = test_results["HAD"][1]

        mae_score,mse_score = get_score_t(feed_dict["graphs"][-1].detach().cpu().x.numpy(),activity_test)
        if epoch_auc_val > best_epoch_val:
            best_epoch_val = epoch_auc_val
            best_epoch_test = epoch_auc_test
            best_epoch_ap = test_ap
            best_epoch_mae = mae_score
            best_epoch_mse = mse_score
            torch.save(model.state_dict(), "./model_checkpoints/model.pt")
            patient = 0
        else:
            patient += 1
            if patient > args.early_stop:
                break

        print("Epoch {:<3},  Loss = {:.3f}, Val AUC {:.3f} Test AUC {:.3f},Test AP {:.3f}, MAE {:.3f},  MSE {:.3f}".format(epoch,
                                                                                   np.mean(epoch_loss),
                                                                                   epoch_auc_val,
                                                                                   epoch_auc_test,
                                                                                   test_ap,
                                                                                   mae_score,
                                                                                   mse_score,))
        wandb.log({"Epoch": epoch,"loss":np.mean(epoch_loss),"Val AUC":epoch_auc_val,"Test AUC":epoch_auc_test,"Test AP":test_ap,"MAE":mae_score,"MSE":mse_score})
    # Test Best Model
    # model.load_state_dict(torch.load("./model_checkpoints/model.pt"))
    # model.eval()
    # emb = model(feed_dict["graphs"],feat)[:, -2, :].detach().cpu().numpy()
    # val_results, test_results, _, _ = evaluate_classifier(train_edges_pos,
    #                                                     train_edges_neg,
    #                                                     val_edges_pos, 
    #                                                     val_edges_neg, 
    #                                                     test_edges_pos,
    #                                                     test_edges_neg, 
    #                                                     emb, 
    #                                                     emb)
    # auc_val = val_results["HAD"][1]
    # auc_test = test_results["HAD"][1]
    print("Best Test AUC={:.3f} Best AP={:.3f} Best MAE={:.3f} Best MSE={:.3f}".format(best_epoch_test,best_epoch_ap,best_epoch_mae,best_epoch_mse))
    wandb.log({"Best Test AUC":best_epoch_test,"Best AP":best_epoch_ap, "Best MAE":best_epoch_mae, "Best MSE":best_epoch_mse})

                







