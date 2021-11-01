# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright 2018-2020 William Falcon
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import pickle
import sys
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import pytest
import torch
from scipy.stats import entropy
from torch.distributions.utils import logits_to_probs
from torch.multiprocessing import Pool, set_start_method
from torchmetrics import Metric

from ..asr.wer_utils import AbstractWEREncoderDecoder, reference_wer_func
from nemo.collections.asr.metrics.wer import WER
from nemo.collections.asr.metrics.wer_bpe import WERBPE
from nemo.collections.common.metrics import GlobalAverageLossMetric, Perplexity

NUM_PROCESSES = 2
NUM_BATCHES = 10
BATCH_SIZE = 16
NUM_CLASSES = 5
EXTRA_DIM = 3
THRESHOLD = 0.5


def setup_ddp(rank, world_size):
    """ Setup ddp enviroment """
    os.environ["MASTER_ADDR"] = 'localhost'
    os.environ['MASTER_PORT'] = '8088'

    if torch.distributed.is_available() and sys.platform not in ['win32', 'cygwin']:
        torch.distributed.init_process_group("gloo", rank=rank, world_size=world_size)


def _class_test(
    rank: int,
    worldsize: int,
    preds: torch.Tensor,
    target: torch.Tensor,
    metric_class: Metric,
    sk_metric: Callable,
    dist_sync_on_step: bool,
    metric_args: dict = None,
    check_dist_sync_on_step: bool = True,
    check_batch: bool = True,
    atol: float = 1e-8,
    reshape_into_2d_tensor_before_passing_to_metric: bool = False,
):
    """ Utility function doing the actual comparison between lightning class metric
        and reference metric.
        Args:
            rank: rank of current process
            worldsize: number of processes
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_class: lightning metric class that should be tested
            sk_metric: callable function that is used for comparison
            dist_sync_on_step: bool, if true will synchronize metric state across
                processes at each ``forward()``
            metric_args: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
            atol: float, the absolute tolerance parameter. See more
                https://numpy.org/doc/stable/reference/generated/numpy.isclose.html
            reshape_into_2d_tensor_before_passing_to_metric: bool, if true stacked metric inputs are reshaped into
                2D. Used if metrics takes only 2D targets and predictions.
    """
    if metric_args is None:
        metric_args = {}
    metric = metric_class(compute_on_step=True, dist_sync_on_step=dist_sync_on_step, **metric_args)

    # verify metrics work after being loaded from pickled state
    pickled_metric = pickle.dumps(metric)
    metric = pickle.loads(pickled_metric)

    for i in range(rank, NUM_BATCHES, worldsize):
        batch_result = metric(preds[i], target[i])

        if metric.dist_sync_on_step:
            if rank == 0:
                ddp_preds = torch.stack([preds[i + r] for r in range(worldsize)])
                ddp_target = torch.stack([target[i + r] for r in range(worldsize)])
                if reshape_into_2d_tensor_before_passing_to_metric:
                    ddp_preds = ddp_preds.reshape([-1, ddp_preds.shape[-1]])
                    ddp_target = ddp_target.reshape([-1, ddp_target.shape[-1]])
                sk_batch_result = sk_metric(ddp_preds, ddp_target)
                # assert for dist_sync_on_step
                if check_dist_sync_on_step:
                    assert np.allclose(batch_result.numpy(), sk_batch_result, atol=atol)
        else:
            sk_batch_result = sk_metric(preds[i], target[i])
            # assert for batch
            if check_batch:
                assert np.allclose(batch_result.numpy(), sk_batch_result, atol=atol)

    # check on all batches on all ranks
    result = metric.compute()
    assert isinstance(result, torch.Tensor)

    total_preds = torch.stack([preds[i] for i in range(NUM_BATCHES)])
    total_target = torch.stack([target[i] for i in range(NUM_BATCHES)])
    if reshape_into_2d_tensor_before_passing_to_metric:
        total_preds = total_preds.reshape([-1, total_preds.shape[-1]])
        total_target = total_target.reshape([-1, total_target.shape[-1]])
    sk_result = sk_metric(total_preds, total_target)

    # assert after aggregation
    assert np.allclose(result.numpy(), sk_result, atol=atol)


def _functional_test(
    preds: torch.Tensor,
    target: torch.Tensor,
    metric_functional: Callable,
    sk_metric: Callable,
    metric_args: dict = {},
    atol: float = 1e-8,
):
    """ Utility function doing the actual comparison between lightning functional metric
        and reference metric.
        Args:
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_functional: lightning metric functional that should be tested
            sk_metric: callable function that is used for comparison
            metric_args: dict with additional arguments used for class initialization
    """
    metric = partial(metric_functional, **metric_args)

    for i in range(NUM_BATCHES):
        lightning_result = metric(preds[i], target[i])
        sk_result = sk_metric(preds[i], target[i])

        # assert its the same
        assert np.allclose(lightning_result.numpy(), sk_result, atol=atol)


class MetricTester:
    """ Class used for efficiently run a lot of parametrized tests in ddp mode.
        Makes sure that ddp is only setup once and that pool of processes are
        used for all tests.
        All tests should subclass from this and implement a new method called
            `test_metric_name`
        where the method `self.run_metric_test` is called inside.
    """

    atol = 1e-8

    def setup_class(self):
        """ Setup the metric class. This will spawn the pool of workers that are
            used for metric testing and setup_ddp
        """
        try:
            set_start_method('spawn')
        except RuntimeError:
            pass
        self.poolSize = NUM_PROCESSES
        self.pool = Pool(processes=self.poolSize)
        self.pool.starmap(setup_ddp, [(rank, self.poolSize) for rank in range(self.poolSize)])

    def teardown_class(self):
        """ Close pool of workers """
        self.pool.close()
        self.pool.join()

    def run_functional_metric_test(
        self,
        preds: torch.Tensor,
        target: torch.Tensor,
        metric_functional: Callable,
        sk_metric: Callable,
        metric_args: dict = {},
    ):
        """
        Main method that should be used for testing functions. Call this inside testing method

        Args:
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_functional: lightning metric class that should be tested
            sk_metric: callable function that is used for comparison
            metric_args: dict with additional arguments used for class initialization
        """
        _functional_test(
            preds=preds,
            target=target,
            metric_functional=metric_functional,
            sk_metric=sk_metric,
            metric_args=metric_args,
            atol=self.atol,
        )

    def run_class_metric_test(
        self,
        ddp: bool,
        preds: torch.Tensor,
        target: torch.Tensor,
        metric_class: Metric,
        sk_metric: Callable,
        dist_sync_on_step: bool,
        metric_args: dict = {},
        check_dist_sync_on_step: bool = True,
        check_batch: bool = True,
        reshape_into_2d_tensor_before_passing_to_metric: bool = False,
    ):
        """
        Main method that should be used for testing class. Call this inside testing methods.

        Args:
            ddp: bool, if running in ddp mode or not
            preds: torch tensor with predictions
            target: torch tensor with targets
            metric_class: lightning metric class that should be tested
            sk_metric: callable function that is used for comparison
            dist_sync_on_step: bool, if true will synchronize metric state across
                processes at each ``forward()``
            metric_args: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
            reshape_into_2d_tensor_before_passing_to_metric: bool, if true stacked metric inputs are reshaped into
                2D. Used if metrics takes only 2D targets and predictions.
        """
        if ddp:
            if sys.platform == "win32":
                pytest.skip("DDP not supported on windows")

            self.pool.starmap(
                partial(
                    _class_test,
                    preds=preds,
                    target=target,
                    metric_class=metric_class,
                    sk_metric=sk_metric,
                    dist_sync_on_step=dist_sync_on_step,
                    metric_args=metric_args,
                    check_dist_sync_on_step=check_dist_sync_on_step,
                    check_batch=check_batch,
                    atol=self.atol,
                    reshape_into_2d_tensor_before_passing_to_metric=reshape_into_2d_tensor_before_passing_to_metric,
                ),
                [(rank, self.poolSize) for rank in range(self.poolSize)],
            )
        else:
            _class_test(
                0,
                1,
                preds=preds,
                target=target,
                metric_class=metric_class,
                sk_metric=sk_metric,
                dist_sync_on_step=dist_sync_on_step,
                metric_args=metric_args,
                check_dist_sync_on_step=check_dist_sync_on_step,
                check_batch=check_batch,
                atol=self.atol,
                reshape_into_2d_tensor_before_passing_to_metric=reshape_into_2d_tensor_before_passing_to_metric,
            )


def reference_perplexity_func(probs):
    ent = entropy(probs, axis=-1)
    ppl = np.exp(ent)
    return ppl.mean()


def _perplexity_class_test(
    rank: int,
    worldsize: int,
    probs: Optional[torch.Tensor],
    logits: Optional[torch.Tensor],
    dist_sync_on_step: bool,
    metric_args: dict = {},
    check_dist_sync_on_step: bool = True,
    check_batch: bool = True,
    atol: float = 1e-8,
):
    """ Utility function doing the actual comparison between lightning class metric
        and reference metric.
        Args:
            rank: rank of current process
            worldsize: number of processes
            probs: torch tensor with probabilities
            logits: torch tensor with logits. The function checks ``probs`` and ``logits are mutually exclusive for
                ``Perplexity`` metric.
            dist_sync_on_step: bool, if true will synchronize metric state across
                processes at each ``forward()``
            metric_args: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
    """
    # Instanciate lightning metric
    perplexity = Perplexity(compute_on_step=True, dist_sync_on_step=dist_sync_on_step, **metric_args)
    if (probs is None) == (logits is None):
        with pytest.raises(ValueError):
            perplexity(probs, logits)
        return

    # verify perplexity works after being loaded from pickled state
    pickled_metric = pickle.dumps(perplexity)
    perplexity = pickle.loads(pickled_metric)

    for i in range(rank, NUM_BATCHES, worldsize):
        batch_result = perplexity(None if probs is None else probs[i], None if logits is None else logits[i])

        if perplexity.dist_sync_on_step:
            if rank == 0:
                if probs is not None:
                    ddp_probs = torch.stack([probs[i + r] for r in range(worldsize)])
                else:
                    ddp_logits = torch.stack([logits[i + r] for r in range(worldsize)])
                    ddp_probs = logits_to_probs(ddp_logits, is_binary=False)
                sk_batch_result = reference_perplexity_func(ddp_probs)
                # assert for dist_sync_on_step
                if check_dist_sync_on_step:
                    assert np.allclose(batch_result.numpy(), sk_batch_result, atol=atol)
        else:
            if probs is None:
                p = logits_to_probs(logits[i], is_binary=False)
            else:
                p = probs[i]
            sk_batch_result = reference_perplexity_func(p)
            # assert for batch
            if check_batch:
                assert np.allclose(batch_result.numpy(), sk_batch_result, atol=atol)

    assert (probs is None) != (logits is None)
    # check on all batches on all ranks
    result = perplexity.compute()
    assert isinstance(result, torch.Tensor)

    if probs is None:
        probs = logits_to_probs(logits, is_binary=False)
    sk_result = reference_perplexity_func(probs)

    # assert after aggregation
    assert np.allclose(result.numpy(), sk_result, atol=atol)


class PerplexityTester(MetricTester):
    def run_class_perplexity_test(
        self,
        ddp: bool,
        probs: Optional[torch.Tensor],
        logits: Optional[torch.Tensor],
        dist_sync_on_step: bool,
        metric_args: dict = {},
        check_dist_sync_on_step: bool = True,
        check_batch: bool = True,
    ):
        """ Main method that should be used for testing class. Call this inside testing
            methods.
            Args:
                ddp: bool, if running in ddp mode or not
                probs: torch tensor with probabilities.
                logits: torch tensor with logits. This test checks that probs and logits are mutually exclusive for
                    ``Perplexity`` metric.
                dist_sync_on_step: bool, if true will synchronize metric state across
                    processes at each ``forward()``
                metric_args: dict with additional arguments used for class initialization
                check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                    calculated per batch per device (and not just at the end)
                check_batch: bool, if true will check if the metric is also correctly
                    calculated across devices for each batch (and not just at the end)
        """
        if ddp:
            if sys.platform == "win32":
                pytest.skip("DDP not supported on windows")

            self.pool.starmap(
                partial(
                    _perplexity_class_test,
                    probs=probs,
                    logits=logits,
                    dist_sync_on_step=dist_sync_on_step,
                    metric_args=metric_args,
                    check_dist_sync_on_step=check_dist_sync_on_step,
                    check_batch=check_batch,
                    atol=self.atol,
                ),
                [(rank, self.poolSize) for rank in range(self.poolSize)],
            )
        else:
            _perplexity_class_test(
                0,
                1,
                probs=probs,
                logits=logits,
                dist_sync_on_step=dist_sync_on_step,
                metric_args=metric_args,
                check_dist_sync_on_step=check_dist_sync_on_step,
                check_batch=check_batch,
                atol=self.atol,
            )


def reference_loss_func(loss_sum_or_avg: torch.Tensor, num_measurements: torch.Tensor, take_avg_loss: bool):
    """
    Returns average loss for data from``loss_sum_or_avg``. This function sums all losses from ``loss_sum_or_avg`` and
    divides the sum by the sum of ``num_measurements`` elements.

    If ``take_avg_loss`` is ``True`` then ``loss_sum_or_avg[i]`` elements are mean values of ``num_measurements[i]``
    losses. In that case before computing sum of losses each element of ``loss_sum_or_avg`` is multiplied by
    corresponding element of ``num_measurements``.

    If ``num_measurements`` sum is zero then the function returns NaN tensor.

    The function is used for testing ``nemo.collections.common.metrics.GlobalAverageLossMetric`` class.

    Args:
        loss_sum_or_avg: a one dimensional float ``torch.Tensor``. Sums or mean values of loss.
        num_measurements: a one dimensional integer ``torch.Tensor``. Number of values on which sums of means in
            ``loss_sum_or_avg`` are calculated.
        take_avg_loss: if ``True`` then ``loss_sum_or_avg`` contains mean losses else ``loss_sum_or_avg`` contains
            sums of losses.
    """
    loss_sum_or_avg = loss_sum_or_avg.clone().detach()
    if take_avg_loss:
        loss_sum_or_avg *= num_measurements
    nm_sum = num_measurements.sum()
    if nm_sum.eq(0):
        return torch.tensor(float('nan'))
    return loss_sum_or_avg.sum() / nm_sum


def _loss_class_test(
    rank: int,
    worldsize: int,
    loss_sum_or_avg: Optional[torch.Tensor],
    num_measurements: Optional[torch.Tensor],
    dist_sync_on_step: bool,
    take_avg_loss: bool,
    check_dist_sync_on_step: bool = True,
    check_batch: bool = True,
    atol: float = 1e-8,
):
    """ Utility function doing the actual comparison between lightning class metric
        and reference metric.
        Args:
            rank: rank of current process
            worldsize: number of processes
            loss_sum_or_avg: a one dimensional float torch tensor with loss sums or means.
            num_measurements: a one dimensional integer torch tensor with number of values on which sums or means from
                ``loss_sum_or_avg`` were computed.
            dist_sync_on_step: bool, if true will synchronize metric state across processes at each call of the
                method :meth:`forward()`
            take_avg_loss: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
    """
    # Instantiate lightning metric
    loss_metric = GlobalAverageLossMetric(
        compute_on_step=True, dist_sync_on_step=dist_sync_on_step, take_avg_loss=take_avg_loss
    )

    # verify loss works after being loaded from pickled state
    pickled_metric = pickle.dumps(loss_metric)
    loss_metric = pickle.loads(pickled_metric)
    for i in range(rank, NUM_BATCHES, worldsize):
        batch_result = loss_metric(loss_sum_or_avg[i], num_measurements[i])
        if loss_metric.dist_sync_on_step:
            if rank == 0:
                ddp_loss_sum_or_avg = torch.stack([loss_sum_or_avg[i + r] for r in range(worldsize)])
                ddp_num_measurements = torch.stack([num_measurements[i + r] for r in range(worldsize)])
                sk_batch_result = reference_loss_func(ddp_loss_sum_or_avg, ddp_num_measurements, take_avg_loss)
                # assert for dist_sync_on_step
                if check_dist_sync_on_step:
                    if sk_batch_result.isnan():
                        assert batch_result.isnan()
                    else:
                        assert np.allclose(
                            batch_result.numpy(), sk_batch_result, atol=atol
                        ), f"batch_result = {batch_result.numpy()}, sk_batch_result = {sk_batch_result}, i = {i}"
        else:
            ls = loss_sum_or_avg[i : i + 1]
            nm = num_measurements[i : i + 1]
            sk_batch_result = reference_loss_func(ls, nm, take_avg_loss)
            # assert for batch
            if check_batch:
                if sk_batch_result.isnan():
                    assert batch_result.isnan()
                else:
                    assert np.allclose(
                        batch_result.numpy(), sk_batch_result, atol=atol
                    ), f"batch_result = {batch_result.numpy()}, sk_batch_result = {sk_batch_result}, i = {i}"
    # check on all batches on all ranks
    result = loss_metric.compute()
    assert isinstance(result, torch.Tensor)
    sk_result = reference_loss_func(loss_sum_or_avg, num_measurements, take_avg_loss)

    # assert after aggregation
    if sk_result.isnan():
        assert result.isnan()
    else:
        assert np.allclose(result.numpy(), sk_result, atol=atol), f"result = {result.numpy()}, sk_result = {sk_result}"


class LossTester(MetricTester):
    def run_class_loss_test(
        self,
        ddp: bool,
        loss_sum_or_avg: torch.Tensor,
        num_measurements: torch.Tensor,
        dist_sync_on_step: bool,
        take_avg_loss: bool,
        check_dist_sync_on_step: bool = True,
        check_batch: bool = True,
    ):
        if ddp:
            if sys.platform == "win32":
                pytest.skip("DDP not supported on windows")
            self.pool.starmap(
                partial(
                    _loss_class_test,
                    loss_sum_or_avg=loss_sum_or_avg,
                    num_measurements=num_measurements,
                    dist_sync_on_step=dist_sync_on_step,
                    take_avg_loss=take_avg_loss,
                    check_dist_sync_on_step=check_dist_sync_on_step,
                    check_batch=check_batch,
                    atol=self.atol,
                ),
                [(rank, self.poolSize) for rank in range(self.poolSize)],
            )
        else:
            _loss_class_test(
                0,
                1,
                loss_sum_or_avg=loss_sum_or_avg,
                num_measurements=num_measurements,
                dist_sync_on_step=dist_sync_on_step,
                take_avg_loss=take_avg_loss,
                check_dist_sync_on_step=check_dist_sync_on_step,
                check_batch=check_batch,
                atol=self.atol,
            )


def _wer_class_test(
    rank: int,
    worldsize: int,
    predictions: torch.Tensor,
    targets: torch.Tensor,
    target_lengths: torch.Tensor,
    predictions_lengths: torch.Tensor,
    wer_class: Union[WER, WERBPE],
    wer_decoder: AbstractWEREncoderDecoder,
    dist_sync_on_step: bool,
    wer_args: Dict[str, Any],
    check_dist_sync_on_step: bool = True,
    check_batch: bool = True,
    atol: float = 1e-8,
):
    """ Utility function doing the actual comparison between lightning class metric
        and reference metric.
        Args:
            rank: rank of current process
            worldsize: number of processes
            loss_sum_or_avg: a one dimensional float torch tensor with loss sums or means.
            num_measurements: a one dimensional integer torch tensor with number of values on which sums or means from
                ``loss_sum_or_avg`` were computed.
            dist_sync_on_step: bool, if true will synchronize metric state across processes at each call of the
                method :meth:`forward()`
            take_avg_loss: dict with additional arguments used for class initialization
            check_dist_sync_on_step: bool, if true will check if the metric is also correctly
                calculated per batch per device (and not just at the end)
            check_batch: bool, if true will check if the metric is also correctly
                calculated across devices for each batch (and not just at the end)
    """
    # Instantiate lightning metric
    import traceback
    log = open(f"rank{rank}.log", 'a')
    try:
        wer_metric = wer_class(compute_on_step=True, dist_sync_on_step=dist_sync_on_step, **wer_args)

        # verify loss works after being loaded from pickled state
        pickled_metric = pickle.dumps(wer_metric)
        wer_metric = pickle.loads(pickled_metric)
        for i in range(rank, NUM_BATCHES, worldsize):
            batch_result = wer_metric(predictions[i], targets[i], target_lengths[i], predictions_lengths[i])
            if wer_metric.dist_sync_on_step:
                if rank == 0:
                    ddp_predictions = torch.cat([predictions[i + r] for r in range(worldsize)])
                    ddp_targets = torch.cat([targets[i + r] for r in range(worldsize)])
                    ddp_target_lengths = torch.cat([target_lengths[i + r] for r in range(worldsize)])
                    ddp_predictions_lengths = torch.cat([predictions_lengths[i + r] for r in range(worldsize)])
                    ref_batch_result = reference_wer_func(
                        ddp_predictions, ddp_targets, ddp_target_lengths, ddp_predictions_lengths, wer_decoder
                    )
                    # assert for dist_sync_on_step
                    if check_dist_sync_on_step:
                        if ref_batch_result.isnan():
                            assert batch_result.isnan()
                        else:
                            assert np.allclose(
                                batch_result.numpy(), ref_batch_result, atol=atol
                            ), f"batch_result = {batch_result.numpy()}, ref_batch_result = {ref_batch_result}, i = {i}"
            else:
                pr = predictions[i]
                tg = targets[i]
                tgl = target_lengths[i]
                prl = predictions_lengths[i]
                ref_batch_result = reference_wer_func(pr, tg, tgl, prl, wer_decoder)
                # assert for batch
                if check_batch:
                    if ref_batch_result.isnan():
                        assert batch_result.isnan()
                    else:
                        assert np.allclose(
                            batch_result.numpy(), ref_batch_result, atol=atol
                        ), f"batch_result = {batch_result.numpy()}, ref_batch_result = {ref_batch_result}, i = {i}"
        # check on all batches on all ranks
        result = wer_metric.compute()
        assert isinstance(result, torch.Tensor)
        predictions = predictions.reshape([-1, predictions.shape[-1]])
        targets = targets.reshape([-1, targets.shape[-1]])
        target_lengths = target_lengths.reshape([-1])
        predictions_lengths = predictions_lengths.reshape([-1])
        ref_result = reference_wer_func(predictions, targets, target_lengths, predictions_lengths, wer_decoder)

        # assert after aggregation
        assert np.allclose(result.numpy(), ref_result, atol=atol), f"result = {result.numpy()}, ref_result = {ref_result}"
    except Exception as e:
        log.write(traceback.format_exc() + '\n' + str(e) + '\n')
        raise


class WERTester(MetricTester):
    def run_class_wer_test(
        self,
        ddp: bool,
        predictions: torch.Tensor,
        targets: torch.Tensor,
        target_lengths: torch.Tensor,
        predictions_lengths: torch.Tensor,
        wer_class: Union[WER, WERBPE],
        wer_decoder: AbstractWEREncoderDecoder,
        dist_sync_on_step: bool,
        wer_args: Dict[str, Any],
        check_dist_sync_on_step: bool = True,
        check_batch: bool = True,
    ):
        class_test_kwargs = dict(
            predictions=predictions,
            targets=targets,
            target_lengths=target_lengths,
            predictions_lengths=predictions_lengths,
            wer_class=wer_class,
            wer_decoder=wer_decoder,
            dist_sync_on_step=dist_sync_on_step,
            wer_args=wer_args,
            check_dist_sync_on_step=check_dist_sync_on_step,
            check_batch=check_batch,
            atol=self.atol,
        )
        if ddp:
            if sys.platform == "win32":
                pytest.skip("DDP not supported on windows")
            self.pool.starmap(
                partial(_wer_class_test, **class_test_kwargs),
                [(rank, self.poolSize) for rank in range(self.poolSize)],
            )
        else:
            _wer_class_test(0, 1, **class_test_kwargs)