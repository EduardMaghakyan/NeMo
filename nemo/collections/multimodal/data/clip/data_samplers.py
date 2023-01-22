import random
import torch
import numpy as np
from multiprocessing import Value
from torch.utils.data import Dataset
from nemo.collections.nlp.data.language_modeling.megatron.megatron_batch_samplers import BaseMegatronBatchSampler
from nemo.collections.vision.data.megatron.vit_dataset import RandomSeedDataset


class SharedEpoch:
    def __init__(self, epoch: int = 0):
        self.shared_epoch = Value('i', epoch)

    def set_value(self, epoch):
        self.shared_epoch.value = epoch

    def get_value(self):
        return self.shared_epoch.value


class WDSUrlsRandomSampler:

    def __init__(
        self,
        dataset: Dataset,
        total_urls: int,
        chunk_size: int,
        consumed_samples: int,
        data_parallel_rank: int,
        data_parallel_size: int,
        drop_last: bool,
        data_sharding: bool,
    ):
        r"""Sampler for WebDataset Urls with data parallelism.
        Args:
            dataset (Dataset): Dataset from which to sample.
            total_urls (int): Total number of urls in the dataset.
            chunk_size (int): Number of objects per tar file.
            consumed_samples (int): Number of samples consumed so far by the training process.
                **Note samples here is not urls.**
            data_parallel_rank (int): Rank of the current data parallel process.
            data_parallel_size (int): Number of data parallel processes.
            drop_last (bool): If True, drop the remaining urls if the number is smaller than `data_parallel_size`.
                If False, pad the urls until its size is divisible by `data_parallel_size`.
            data_sharding (bool): If True, use data sharding before data shuffling, i.e. only shuffle within the data parallel group.
        """
        self.dataset = dataset
        self.total_urls = total_urls
        self.chunk_size = chunk_size
        self.consumed_samples = consumed_samples
        assert consumed_samples % data_parallel_size == 0
        self.consumed_urls = consumed_samples // data_parallel_size // chunk_size * data_parallel_size

        self.data_parallel_rank = data_parallel_rank
        self.data_parallel_size = data_parallel_size
        self.drop_last = drop_last
        self.data_sharding = data_sharding
        self.epoch = SharedEpoch()

        self.remaining_urls =  self.total_urls % self.data_parallel_size

    def __len__(self):
        if self.drop_last:
            return self.total_urls // self.data_parallel_size
        else:
            return (self.total_urls + self.data_parallel_size - 1) // self.data_parallel_size

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is not None:
            worker_id, num_workers = worker_info.id, worker_info.num_workers

        self.consumed_urls = self.consumed_samples // self.data_parallel_size \
                             // self.chunk_size * self.data_parallel_size

        if self.drop_last or self.remaining_urls == 0:
            active_total_urls = self.total_urls - self.remaining_urls
        else:
            active_total_urls = self.total_urls + self.data_parallel_size - self.remaining_urls

        self.epoch.set_value(self.consumed_urls // active_total_urls)
        current_epoch_urls = self.consumed_urls % active_total_urls

        if isinstance(self.dataset, RandomSeedDataset):
            self.dataset.set_epoch(self.epoch.get_value())

        # data sharding and random sampling
        if self.data_sharding:
            bucket_size = active_total_urls // self.data_parallel_size
            bucket_offset = current_epoch_urls // self.data_parallel_size
            start_idx = self.data_parallel_rank * bucket_size

            g = torch.Generator()
            g.manual_seed(self.epoch.get_value())
            random_idx = torch.randperm(bucket_size, generator=g).tolist()
            idx_range = [start_idx + x for x in random_idx[bucket_offset:]]
        else:
            full_bucket_size = active_total_urls
            full_bucket_offset = current_epoch_urls
            g = torch.Generator()
            g.manual_seed(self.epoch.get_value())
            idx_range_total = \
                torch.randperm(full_bucket_size, generator=g).tolist()
            idx_range_active = idx_range_total[full_bucket_offset:]
            idx_range = idx_range_active[self.data_parallel_rank::self.data_parallel_size]

        # Use additional permutation to replace out-of-range indices when drop_last is False
        additional_random_idx = torch.randperm(self.total_urls, generator=g).tolist()
        for n, idx in enumerate(idx_range):
            self.consumed_samples += self.data_parallel_size * self.chunk_size
            if worker_info is not None and n % num_workers != worker_id:
                continue
            if idx < self.total_urls:
                yield idx
            else:
                yield additional_random_idx[idx - self.total_urls]