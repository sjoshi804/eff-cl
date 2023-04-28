from typing import Dict, Optional
import math 

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset

from base_subset_dataset import BaseSubsetDataset
from submodular_maximization import lazy_greedy


class SubsetSelectionObjective:
    def __init__(self, distance, threshold=0):
        '''
        :param distance: (n, n) matrix specifying pairwise augmentation distance
        :type distance: np.array
        :param threshold: minimum cosine similarity to consider to be significant (default=0)
        :type threshold: float
        '''
        self.distance = distance 
        self.threshold = threshold

    def inc(self, sset, i):
        return np.sum(self.distance[i] * (self.distance[i] > 0)) - np.sum(self.distance[np.ix_(sset, [i])])
    
    def add(self, i):
        self.distance[:][i] = 0
        return 
    
class CLCoreSubsetDataset(BaseSubsetDataset):
    def __init__(
        self,
        dataset: Dataset,
        subset_fraction: float,
        num_downstream_classes: int,
        device: torch.device,
        proxy_model: nn.Module,
        approx_latent_class_partition: Dict[int, int],
        augmentation_distance: Optional[Dict[int, np.array()]] = None,
        num_augmentations=1,
        pairwise_distance_block_size: int = 1024, 
        verbose: bool = False
    ):
        """
        :param dataset: original dataset for contrastive learning. assumes that dataset[i] = returns a list of augmented views of original example i. 
        :type dataset: Dataset
        :param subset_fraction: Fractional size of subset
        :type subset_fraction: float
        :param num_downstream_classes: number of downstream classes (can be estimate)
        :type num_downstream_classes: int
        :param proxy_model: proxy model to calculate augmentation distance (and kmeans clustering if avoid clip option chosen)
        :type proxy_model: nn.Module
        :param augmentation_distance: Pass precomputed dictionary containing augmentation distance for each latent class
        :type augmentation_distance: Dict[int, np.array]
        :param num_augmentations: Number of augmentations to consider while approximating augmentation distance
        :type num_augmentations: int
        :param pairwise_distance_block_size: block size for calculating pairwise distance. this is just to optimize GPU usage while calculating pairwise distance and will not affect the subset created in anyway. 
        :type pairwise_distance_block_size: int
        :param verbose: verbose
        :type verbose: boolean
        """
        super().__init__(
            dataset=dataset, 
            subset_fraction=subset_fraction,
            verbose=verbose
        )
        self.device = device
        self.num_downstream_classes = num_downstream_classes
        self.proxy_model = proxy_model
        self.partition = approx_latent_class_partition
        self.augmentation_distance = augmentation_distance
        self.num_augmentations = num_augmentations
        self.pairwise_distance_block_size = pairwise_distance_block_size

        if self.augmentation_distance == None:
            self.augmentation_distance = self.approximate_augmentation_distance()

        class_wise_idx = {}
        for latent_class in self.partition.keys():
            F = SubsetSelectionObjective(self.augmentation_distance[latent_class].copy())
            class_wise_idx[latent_class] = lazy_greedy(F, range(len(self.augmentation_distance[latent_class])), len(self.augmentation_distance[latent_class]))
            class_wise_idx[latent_class] = [self.partition[latent_class][i] for i in class_wise_idx[latent_class]]
            
        self.subset_indices = []
        for latent_class in class_wise_idx.keys():
            l = len(class_wise_idx[latent_class])
            self.subset_indices.extend(class_wise_idx[latent_class][:int(self.subset_fraction * l)])

        self.initialization_complete()


    def approximate_augmentation_distance(self):
        self.proxy_model = self.proxy_model.to(self.device)

        # Initialize augmentation distance with all 0s
        augmentation_distance = {}
        for latent_class in self.partition.keys():
            augmentation_distance[latent_class] = np.zeros((len(self.partition[latent_class]), len(self.partition[latent_class])))

        num_positives = len(self.dataset[i])
        num_runs = math.ceil(self.num_augmentations / len(self.dataset[i]))

        # If considering only 1 augmentation for speed (this approach works when the proxy model has good alignment 
        # i.e. most augmentations of an example have embeddings that are similar to the embeddings of the other examples)
        if self.num_augmentations == 1:
            Z = self.encode_augmented_trainset()
            for latent_class in self.partition.keys():
                Z_partition = torch.cat(
                    [Z[[i + len(self.partition[latent_class]) * pos_num for i in self.partition[latent_class]]] 
                     for pos_num in range(num_positives)]
                )
                pairwise_distance = CLCoreSubsetDataset.pairwise_distance(Z_partition, Z_partition)
                augmentation_distance[latent_class] += pairwise_distance[np.ix_(rows, cols)] 
            return augmentation_distance

        for _ in num_runs:
            if self.num_augmentations == 1:
                Z = self.encode_augmented_trainset()

            Z = self.encode_augmented_trainset(num_positives=num_positives)
            for latent_class in self.partition.keys():
                Z_partition = torch.cat(
                    [Z[[i + len(self.partition[latent_class]) * pos_num for i in self.partition[latent_class]]] 
                     for pos_num in range(num_positives)]
                )
                pairwise_distance = CLCoreSubsetDataset.pairwise_distance(Z_partition, Z_partition)
                for i in range(num_positives):
                    rows = [range(self.len_dataset * i, self.len_dataset * (i + 1))]
                    for j in range(i + 1, num_positives):
                        cols = [range(self.len_dataset * j, self.len_dataset * (j + 1))]
                        augmentation_distance[latent_class] += pairwise_distance[np.ix_(rows, cols)] / (i * (i - 1))

        return augmentation_distance

    def encode_trainset(self):
        trainloader = torch.utils.data.DataLoader(self.dataset, batch_size=self.pairwise_distance_block_size, shuffle=False, num_workers=2, pin_memory=True)
        with torch.no_grad():
            Z = []
            for input in enumerate(trainloader):
                Z.append(self.proxy_model(input[0].to(self.device)))
        return Z
    
    def encode_augmented_trainset(self, num_positives):
        trainloader = torch.utils.data.DataLoader(self.dataset, batch_size=self.pairwise_distance_block_size, shuffle=False, num_workers=2, pin_memory=True)
        with torch.no_grad():
            Z = [[]] * num_positives
            for input in enumerate(trainloader):
                for i in range(num_positives):
                    Z[i].append(self.proxy_model(input[i].to(self.device)))
        return Z

    @staticmethod
    def pairwise_distance(Z1: torch.tensor, Z2: torch.tensor, block_size: int):
        similarity_matrices = []
        for i in range(Z1.shape[0] // block_size + 1)):
            similarity_matrices_i = []
            e = Z1[i*block_size:(i+1)*block_size]
            for j in range(Z2.shape[0] // block_size + 1):
                e_t = Z2[j*block_size:(j+1)*block_size].t()
                similarity_matrices_i.append(
                    np.array(
                    torch.cosine_similarity(e[:, :, None], e_t[None, :, :]).detach().cpu()
                    )
                )
            similarity_matrices.append(similarity_matrices_i)
        similarity_matrix = np.block(similarity_matrices)

        return similarity_matrix


