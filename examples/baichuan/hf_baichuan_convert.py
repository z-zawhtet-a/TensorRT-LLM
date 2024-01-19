# SPDX-FileCopyrightText: Copyright (c) 2022-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
'''
Convert Baichuan models. Use https://huggingface.co/baichuan-inc/Baichuan2-7B-Chat as demo.
'''
import argparse
import configparser
import os
import platform
from pathlib import Path

import torch
import torch.multiprocessing as multiprocessing
from convert import split_and_save_weight, str_to_np_dtype
from smoothquant import (capture_activation_range, smooth_gemm,
                         smooth_gemm_fc1_gate)
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer


@torch.no_grad()
def smooth_baichuan_model(model, scales, alpha, baichuan_smoother):
    # Smooth the activation and weights with smoother = $\diag{s}$
    for name, module in model.named_modules():
        class_name = module.__class__.__name__
        if not 'Layer' in class_name:
            continue
        print(f'smoothing module: {name}, class_name: {class_name}')
        # qkv_proj
        layer_name_qkv = name + ".self_attn.W_pack"

        smoother = smooth_gemm(module.self_attn.W_pack.weight,
                               scales[layer_name_qkv]["x"],
                               module.input_layernorm.weight, None, alpha)

        scales[layer_name_qkv]["x"] = scales[layer_name_qkv]["x"] / smoother
        scales[layer_name_qkv]["w"] = module.self_attn.W_pack.weight.abs().max(
            dim=1)[0]

        # =================================================================
        layer_name = name + ".self_attn.o_proj"
        smoother = smooth_gemm(module.self_attn.o_proj.weight,
                               scales[layer_name]["x"], None, None, alpha)
        baichuan_smoother[layer_name] = smoother.float()

        scales[layer_name]["x"] = scales[layer_name]["x"] / smoother
        scales[layer_name]["w"] = module.self_attn.o_proj.weight.abs().max(
            dim=1)[0]

        # ==================================================================
        fc1_layer_name = name + ".mlp.gate_proj"
        gate_layer_name = name + ".mlp.up_proj"

        smoother = smooth_gemm_fc1_gate(module.mlp.gate_proj.weight,
                                        module.mlp.up_proj.weight,
                                        scales[fc1_layer_name]["x"],
                                        module.post_attention_layernorm.weight,
                                        None, alpha)

        scales[fc1_layer_name]["x"] = scales[fc1_layer_name]["x"] / smoother
        scales[fc1_layer_name]["w"] = module.mlp.gate_proj.weight.abs().max(
            dim=1)[0]

        scales[gate_layer_name]["x"] = scales[gate_layer_name]["x"] / smoother
        scales[gate_layer_name]["w"] = module.mlp.up_proj.weight.abs().max(
            dim=1)[0]

        # ==================================================================
        layer_name = name + ".mlp.down_proj"
        smoother = smooth_gemm(module.mlp.down_proj.weight,
                               scales[layer_name]["x"], None, None, alpha)
        baichuan_smoother[layer_name] = smoother.float()
        scales[layer_name]["x"] = scales[layer_name]["x"] / smoother
        scales[layer_name]["w"] = module.mlp.down_proj.weight.abs().max(
            dim=1)[0]


def baichuan_to_bin_name(orig_name):
    global_bin_weights = {
        "model.embed_tokens.weight": 'vocab_embedding.weight',
        "model.norm.weight": 'ln_f.weight',
        "lm_head.weight": 'lm_head.weight',
    }

    if orig_name in global_bin_weights:
        return global_bin_weights[orig_name]

    _, _, layer_id, *weight_name = orig_name.split(".")

    layer_id = int(layer_id)
    weight_name = ".".join(weight_name)

    per_layer_weights = {
        "input_layernorm.weight": "input_layernorm.weight",
        "self_attn.W_pack.weight": "attention.query_key_value.weight",
        "self_attn.o_proj.weight": "attention.dense.weight",
        "mlp.gate_proj.weight": "mlp.fc.weight",
        "mlp.down_proj.weight": "mlp.proj.weight",
        "mlp.up_proj.weight": "mlp.gate.weight",
        "post_attention_layernorm.weight": "post_layernorm.weight",
    }

    return f"layers.{layer_id}.{per_layer_weights[weight_name]}"


# Baichuan uses nn.Linear for these following ops whose weight matrix is transposed compared to gpt2.
# In order to use the preprocess codes of gpt2, we transpose them firstly.
def transpose_weights(hf_name, param):
    weight_to_transpose = [
        "W_pack", "o_proj", "gate_proj", "down_proj", "up_proj"
    ]
    if any([k in hf_name for k in weight_to_transpose]):
        if len(param.shape) == 2:
            param = param.transpose(0, 1)
    return param


def hf_baichuan_converter(args):
    infer_tp = args.tensor_parallelism
    saved_dir = Path(args.out_dir) / f"{infer_tp}-gpu"
    saved_dir.mkdir(parents=True, exist_ok=True)

    model = AutoModelForCausalLM.from_pretrained(args.in_file,
                                                 torch_dtype=torch.float16,
                                                 device_map="auto",
                                                 trust_remote_code=True)

    act_range = {}
    # smoother for inputs of self_attn.o_proj and mlp.down_proj
    baichuan_smoother = {}

    if args.smoothquant is not None or args.calibrate_kv_cache:
        os.environ["TOKENIZERS_PARALLELISM"] = os.environ.get(
            "TOKENIZERS_PARALLELISM", "false")
        act_range = capture_activation_range(
            model,
            AutoTokenizer.from_pretrained(args.in_file,
                                          use_fast=False,
                                          trust_remote_code=True))
        if args.smoothquant is not None:
            smooth_baichuan_model(model, act_range, args.smoothquant,
                                  baichuan_smoother)

    config = configparser.ConfigParser()
    config["baichuan"] = {}
    for key in vars(args):
        config["baichuan"][key] = f"{vars(args)[key]}"
    for k, v in vars(model.config).items():
        config["baichuan"][k] = f"{v}"
    config["baichuan"]["weight_data_type"] = args.storage_type
    config["baichuan"]["multi_query_mode"] = str(False)
    with open(saved_dir / "config.ini", 'w') as configfile:
        config.write(configfile)

    storage_type = str_to_np_dtype(args.storage_type)

    global_bin_weights = [
        'vocab_embedding.weight', 'ln_f.weight', 'lm_head.weight'
    ]

    int8_outputs = None
    if args.calibrate_kv_cache:
        int8_outputs = "kv_cache_only"
    if args.smoothquant is not None:
        int8_outputs = "all"

    starmap_args = []
    for name, param in model.named_parameters():
        if "weight" not in name and "bias" not in name:
            continue
        bin_name = baichuan_to_bin_name(name)

        if name.replace(".weight", "") in baichuan_smoother.keys():
            smoother = baichuan_smoother[name.replace(".weight", "")]
            smoother = smoother.detach().cpu().numpy()
            starmap_args.append(
                (0, saved_dir, infer_tp,
                 f"{bin_name}.smoother".replace(".weight",
                                                ""), smoother, None, {
                                                    "int8_outputs":
                                                    int8_outputs,
                                                    "multi_query_mode": False,
                                                    "local_dim": None,
                                                }))

        param = transpose_weights(name, param)

        param = param.detach().cpu().numpy().astype(storage_type)

        if bin_name in global_bin_weights:
            param.tofile(saved_dir / f"{bin_name}.bin")
        elif bin_name.split('.')[-2] == 'query_key_value':
            local_dim = None
            layer_name_qkv = name.replace(".weight", "")
            # Baichuan models use W_pack to transform qkv
            # So we can simply use param as qkv weight here
            qkv = (0, saved_dir, infer_tp, bin_name, param,
                   act_range.get(layer_name_qkv), {
                       "int8_outputs": int8_outputs,
                       "multi_query_mode": False,
                       "local_dim": local_dim,
                   })
            starmap_args.append(qkv)
        elif bin_name.split('.')[-2] == 'kv':
            continue
        else:
            starmap_args.append((0, saved_dir, infer_tp, bin_name, param,
                                 act_range.get(name.replace(".weight", "")), {
                                     "int8_outputs": int8_outputs,
                                     "multi_query_mode": False,
                                     "local_dim": None,
                                 }))

    starmap_args = tqdm(starmap_args, desc="saving weights")
    if args.processes > 1:
        with multiprocessing.Pool(args.processes) as pool:
            pool.starmap(split_and_save_weight, starmap_args)
    else:
        # simpler for debug situations
        for starmap_arg in starmap_args:
            split_and_save_weight(*starmap_arg)


if __name__ == "__main__":
    torch.multiprocessing.set_start_method("spawn")

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--out-dir',
                        '-o',
                        type=str,
                        help='file name of output directory',
                        required=True)
    parser.add_argument('--in-file',
                        '-i',
                        type=str,
                        help='file name of input checkpoint file',
                        required=True)
    parser.add_argument('--tensor-parallelism',
                        '-tp',
                        type=int,
                        help='Requested tensor parallelism for inference',
                        default=1)
    parser.add_argument(
        "--processes",
        "-p",
        type=int,
        help="How many processes to spawn for conversion (default: 4)",
        default=4)
    parser.add_argument(
        "--calibrate-kv-cache",
        "-kv",
        action="store_true",
        help=
        "Generate scaling factors for KV cache. Used for storing KV cache in int8."
    )
    parser.add_argument(
        "--smoothquant",
        "-sq",
        type=float,
        default=None,
        help="Set the α parameter (see https://arxiv.org/pdf/2211.10438.pdf)"
        " to Smoothquant the model, and output int8 weights."
        " A good first try is 0.5. Must be in [0, 1]")
    parser.add_argument("--storage-type",
                        "-t",
                        type=str,
                        default="fp32",
                        choices=["fp32", "fp16"])

    args = parser.parse_args()
    if args.processes > 1 and platform.system() == "Windows":
        print(
            "Resetting processes to 1 because multi-process on Windows is not implemented."
        )
        args.processes = 1

    print("\n=============== Argument ===============")
    for key in vars(args):
        print("{}: {}".format(key, vars(args)[key]))
    print("========================================")

    assert (args.calibrate_kv_cache or args.smoothquant), \
        ("Either INT8 kv cache or SmoothQuant must be enabled for this script. "
        "Otherwise you can directly build engines from HuggingFace checkpoints,"
        " no need to do this bin format conversion. ")
    hf_baichuan_converter(args)
