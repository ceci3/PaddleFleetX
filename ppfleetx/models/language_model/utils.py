# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

import logging
import os
import sys
import copy

import yaml
import numpy as np
import paddle
import paddle.distributed as dist
from paddle.fluid import core
import argparse
from functools import reduce

from ppfleetx.utils import env
from ppfleetx.utils.log import logger


def is_fused_matmul_bias_supported():
    if paddle.is_compiled_with_cuda() and not paddle.is_compiled_with_rocm():
        return hasattr(core.ops, 'fused_gemm_epilogue')
    else:
        return False


def process_inference_configs(config):
    """
    process inference configs for hybrid parallel
    """
    if 'Inference' not in config.keys():
        return

    configs = config['Inference']

    if configs['model_dir'] is None:
        configs['model_dir'] = config['Engine']['save_load']['output_dir']

    if configs['mp_degree'] is None:
        configs['mp_degree'] = config['Distributed']['mp_degree']


def process_model_configs(config):
    """
    process model configs for hybrid parallel
    """
    configs = config['Model']
    if configs['ffn_hidden_size'] is None:
        configs['ffn_hidden_size'] = 4 * configs['hidden_size']

    if configs['use_recompute']:
        if not configs['recompute_granularity']:
            configs['recompute_granularity'] = 'full'

    if configs['fused_linear'] and not is_fused_matmul_bias_supported():
        configs['fused_linear'] = False
        logging.warning(
            "The flag fused_linear only valid for cuda version higher than 11.6, "
            "but the paddle is compiled with cuda " + paddle.version.cuda())

    pp_degree = config.Distributed.pp_degree

    if pp_degree > 1:
        configs['virtual_pp_degree'] = 1 \
            if configs.get('virtual_pp_degree', None) is None \
            else configs['virtual_pp_degree']
        virtual_pp_degree = configs['virtual_pp_degree']
        num_layers = configs.num_layers

        assert (num_layers %
            (virtual_pp_degree * pp_degree)) == 0, \
            "The num_layers of the model should be divisible of pp_degree * virtual_pp_degree." \
            "Receive num_layers: {}, pp_degree: {}, virtual_pp_degree: {}.".format(
            num_layers, pp_degree, virtual_pp_degree)

        if virtual_pp_degree > 1:
            local_batch_size = config.Global.local_batch_size
            micro_batch_size = config.Global.micro_batch_size
            acc_steps = local_batch_size // micro_batch_size
            assert acc_steps % pp_degree == 0, "num of microbatches {} should be divisible of pp_degree {} when " \
                                               "using interleave pipeline".format(acc_steps, pp_degree)

        if virtual_pp_degree > 2:
            logger.warning(
                "Setting virtual_pp_degree > 2 may harm the throughput of the pipeline parallel."
            )
    else:
        if configs.get('virtual_pp_degree', None):
            logger.warning("virtual_pp_degree is unuseful.")


def process_optim_configs(config):
    """
    process optim configs for hybrid parallel
    """
    config['Optimizer']['multi_precision'] = config['Engine']['mix_precision'][
        'use_pure_fp16']

    nranks = dist.get_world_size()
    dp_degree = config['Distributed']['dp_degree']
    if config['Optimizer']['tensor_fusion']:
        assert nranks == dp_degree, "tensor_fusion only support single card train or data parallel train"


def process_data_configs(config):
    """
    process data configs for hybrid parallel
    """
    cfg_global = config['Global']
    cfg_data = config['Data']

    mode_to_num_samples = {
        "Train":
        cfg_global['global_batch_size'] * config['Engine']['max_steps'],
        "Eval": cfg_global['global_batch_size'] *
        (config['Engine']['max_steps'] // config['Engine']['eval_freq'] + 1) *
        config['Engine']['eval_iters'],
        "Test":
        cfg_global['global_batch_size'] * config['Engine']['test_iters'],
    }

    for mode in ("Train", "Eval", "Test"):
        if mode in cfg_data.keys():
            cfg_data[mode]['dataset']['num_samples'] = mode_to_num_samples[
                mode]
            cfg_data[mode]['dataset']['mode'] = mode
            cfg_data[mode]['dataset']['seed'] = cfg_global['seed']
            cfg_data[mode]['sampler']['batch_size'] = cfg_global[
                'local_batch_size']


def process_configs(config):
    process_data_configs(config)
    process_model_configs(config)
    process_optim_configs(config)
    process_inference_configs(config)

    return config
