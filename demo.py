# Copyright (c) Aishwarya Kamath & Nicolas Carion. Licensed under the Apache License 2.0. All Rights Reserved
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import json
import os
import random
import time
from collections import namedtuple
from copy import deepcopy
from functools import partial
from pathlib import Path
from torchvision.transforms import Compose, ToTensor, Normalize

import numpy as np
import torch
import torch.utils
from torch.utils.data import ConcatDataset, DataLoader, DistributedSampler
from IPython import embed
import util.dist as dist
import util.misc as utils
from engine import evaluate, train_one_epoch
from models import build_model
from models.postprocessors import build_postprocessors
from datasets.yourefit import ReferDataset, YouRefItEvaluator
from datasets.coco import make_coco_transforms

def get_args_parser():
    parser = argparse.ArgumentParser("Set transformer detector", add_help=False)
    parser.add_argument("--run_name", default="", type=str)

    # Dataset specific
    parser.add_argument("--dataset_config", default=None, required=True)
    parser.add_argument("--do_qa", action="store_true", help="Whether to do question answering")
    parser.add_argument(
        "--predict_final",
        action="store_true",
        help="If true, will predict if a given box is in the actual referred set. Useful for CLEVR-Ref+ only currently.",
    )
    parser.add_argument("--no_detection", action="store_true", help="Whether to train the detector")
    parser.add_argument(
        "--split_qa_heads", action="store_true", help="Whether to use a separate head per question type in vqa"
    )
    parser.add_argument(
        "--combine_datasets", nargs="+", help="List of datasets to combine for training", default=["flickr"]
    )
    parser.add_argument(
        "--combine_datasets_val", nargs="+", help="List of datasets to combine for eval", default=["flickr"]
    )

    parser.add_argument("--coco_path", type=str, default="")
    parser.add_argument("--vg_img_path", type=str, default="")
    parser.add_argument("--vg_ann_path", type=str, default="")
    parser.add_argument("--clevr_img_path", type=str, default="")
    parser.add_argument("--clevr_ann_path", type=str, default="")
    parser.add_argument("--phrasecut_ann_path", type=str, default="")
    parser.add_argument(
        "--phrasecut_orig_ann_path",
        type=str,
        default="",
    )
    parser.add_argument("--modulated_lvis_ann_path", type=str, default="")

    # Training hyper-parameters
    parser.add_argument("--lr", default=1e-4, type=float)
    parser.add_argument("--lr_backbone", default=1e-5, type=float)
    parser.add_argument("--text_encoder_lr", default=5e-5, type=float)
    parser.add_argument("--batch_size", default=2, type=int)
    parser.add_argument("--weight_decay", default=1e-4, type=float)
    parser.add_argument("--epochs", default=40, type=int)
    parser.add_argument("--lr_drop", default=35, type=int)
    parser.add_argument(
        "--epoch_chunks",
        default=-1,
        type=int,
        help="If greater than 0, will split the training set into chunks and validate/checkpoint after each chunk",
    )
    parser.add_argument("--optimizer", default="adam", type=str)
    parser.add_argument("--clip_max_norm", default=0.1, type=float, help="gradient clipping max norm")
    parser.add_argument(
        "--eval_skip",
        default=1,
        type=int,
        help='do evaluation every "eval_skip" frames',
    )

    parser.add_argument(
        "--schedule",
        default="linear_with_warmup",
        type=str,
        choices=("step", "multistep", "linear_with_warmup", "all_linear_with_warmup"),
    )
    parser.add_argument("--ema", action="store_true")
    parser.add_argument("--ema_decay", type=float, default=0.9998)
    parser.add_argument("--fraction_warmup_steps", default=0.01, type=float, help="Fraction of total number of steps")

    # Model parameters
    parser.add_argument(
        "--frozen_weights",
        type=str,
        default=None,
        help="Path to the pretrained model. If set, only the mask head will be trained",
    )

    parser.add_argument(
        "--freeze_text_encoder", action="store_true", help="Whether to freeze the weights of the text encoder"
    )

    parser.add_argument(
        "--text_encoder_type",
        default="roberta-base",
        choices=("roberta-base", "distilroberta-base", "roberta-large"),
    )

    # Backbone
    parser.add_argument(
        "--backbone",
        default="resnet101",
        type=str,
        help="Name of the convolutional backbone to use such as resnet50 resnet101 timm_tf_efficientnet_b3_ns",
    )
    parser.add_argument(
        "--dilation",
        action="store_true",
        help="If true, we replace stride with dilation in the last convolutional block (DC5)",
    )
    parser.add_argument(
        "--position_embedding",
        default="sine",
        type=str,
        choices=("sine", "learned"),
        help="Type of positional embedding to use on top of the image features",
    )

    # Transformer
    parser.add_argument(
        "--enc_layers",
        default=6,
        type=int,
        help="Number of encoding layers in the transformer",
    )
    parser.add_argument(
        "--dec_layers",
        default=6,
        type=int,
        help="Number of decoding layers in the transformer",
    )
    parser.add_argument(
        "--dim_feedforward",
        default=2048,
        type=int,
        help="Intermediate size of the feedforward layers in the transformer blocks",
    )
    parser.add_argument(
        "--hidden_dim",
        default=256,
        type=int,
        help="Size of the embeddings (dimension of the transformer)",
    )
    parser.add_argument("--dropout", default=0.1, type=float, help="Dropout applied in the transformer")
    parser.add_argument(
        "--nheads",
        default=8,
        type=int,
        help="Number of attention heads inside the transformer's attentions",
    )
    parser.add_argument("--num_queries", default=20, type=int, help="Number of query slots")
    parser.add_argument("--pre_norm", action="store_true")
    parser.add_argument(
        "--no_pass_pos_and_query",
        dest="pass_pos_and_query",
        action="store_false",
        help="Disables passing the positional encodings to each attention layers",
    )

    # Segmentation
    parser.add_argument(
        "--mask_model",
        default="none",
        type=str,
        choices=("none", "smallconv", "v2"),
        help="Segmentation head to be used (if None, segmentation will not be trained)",
    )
    parser.add_argument("--remove_difficult", action="store_true")
    parser.add_argument("--masks", action="store_true")

    # Loss
    parser.add_argument(
        "--no_aux_loss",
        dest="aux_loss",
        action="store_false",
        help="Disables auxiliary decoding losses (loss at each layer)",
    )
    parser.add_argument(
        "--set_loss",
        default="hungarian",
        type=str,
        choices=("sequential", "hungarian", "lexicographical"),
        help="Type of matching to perform in the loss",
    )

    parser.add_argument("--contrastive_loss", action="store_true", help="Whether to add contrastive loss")
    parser.add_argument(
        "--no_contrastive_align_loss",
        dest="contrastive_align_loss",
        action="store_false",
        help="Whether to add contrastive alignment loss",
    )

    parser.add_argument(
        "--contrastive_loss_hdim",
        type=int,
        default=64,
        help="Projection head output size before computing normalized temperature-scaled cross entropy loss",
    )

    parser.add_argument(
        "--temperature_NCE", type=float, default=0.07, help="Temperature in the  temperature-scaled cross entropy loss"
    )

    # * Matcher
    parser.add_argument(
        "--set_cost_class",
        default=1,
        type=float,
        help="Class coefficient in the matching cost",
    )
    parser.add_argument(
        "--set_cost_bbox",
        default=5,
        type=float,
        help="L1 box coefficient in the matching cost",
    )
    parser.add_argument(
        "--set_cost_giou",
        default=2,
        type=float,
        help="giou box coefficient in the matching cost",
    )
    # Loss coefficients
    parser.add_argument("--ce_loss_coef", default=1, type=float)
    parser.add_argument("--mask_loss_coef", default=1, type=float)
    parser.add_argument("--dice_loss_coef", default=1, type=float)
    parser.add_argument("--bbox_loss_coef", default=5, type=float)
    parser.add_argument("--giou_loss_coef", default=2, type=float)
    parser.add_argument("--qa_loss_coef", default=1, type=float)
    parser.add_argument(
        "--eos_coef",
        default=0.1,
        type=float,
        help="Relative classification weight of the no-object class",
    )
    parser.add_argument("--contrastive_loss_coef", default=0.1, type=float)
    parser.add_argument("--contrastive_align_loss_coef", default=1, type=float)

    # Run specific

    parser.add_argument("--test", action="store_true", help="Whether to run evaluation on val or test set")
    parser.add_argument("--test_type", type=str, default="test", choices=("testA", "testB", "test"))
    parser.add_argument("--output-dir", default="", help="path where to save, empty for no saving")
    parser.add_argument("--device", default="cuda", help="device to use for training / testing")
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--resume", default="", help="resume from checkpoint")
    parser.add_argument("--load", default="", help="resume from checkpoint")
    parser.add_argument("--start-epoch", default=0, type=int, metavar="N", help="start epoch")
    parser.add_argument("--eval", action="store_true", help="Only run evaluation")
    parser.add_argument("--num_workers", default=5, type=int)

    # Distributed training parameters
    parser.add_argument("--world-size", default=1, type=int, help="number of distributed processes")
    parser.add_argument("--dist-url", default="env://", help="url used to set up distributed training")
    return parser


import math
import sys
from typing import Dict, Iterable, Optional

import torch
import torch.nn
import torch.optim
from datasets.yourefit import YouRefItEvaluator

import util.dist as dist
from datasets.clevrref import ClevrRefEvaluator
from datasets.coco_eval import CocoEvaluator
from datasets.flickr_eval import FlickrEvaluator
#from datasets.phrasecut_eval import PhrasecutEvaluator
from datasets.refexp import RefExpEvaluator
from util.metrics import MetricLogger, SmoothedValue
from util.misc import targets_to
from util.optim import adjust_learning_rate, update_ema
from IPython import embed
import time
from models.mdetr import get_pose_loss

def demo(
    model: torch.nn.Module,
    postprocessors: Dict[str, torch.nn.Module],
    data_loader,
    device: torch.device,
    args,
):
    model.eval()

    metric_logger = MetricLogger(delimiter="  ")
    header = "Test:"
    
    for batch_dict in metric_logger.log_every(data_loader, 10, header, args.output_dir):
        samples = batch_dict["samples"].to(device)
        targets = batch_dict["targets"]
        captions = [t["caption"] for t in targets]

        targets = targets_to(targets, device)

        pafs = None
        yourefit = True
        target_arms = None
        if yourefit:
            paf_list = []
            target_arms = []
            for target in targets:
                paf_list.append(target['ht_map'])
                if target['arm'].shape[0]==4:
                    target['arm'] = target['arm'].unsqueeze(0)
                target_arms.append(target['arm'])
            from util.misc import NestedTensor
            pafs =  NestedTensor.from_tensor_list(paf_list)

        memory_cache = None
        target_arm = None
        pred_arm = None
        pose_out = None
        if args.masks:
            outputs = model(samples, captions)
        else:
            memory_cache, pose_out = model(samples, captions, encode_and_save=True,paf_samples=pafs)
            outputs = model(samples, captions, encode_and_save=False, memory_cache=memory_cache,arm_query=target_arm)
            if pose_out is not None:
                outputs.update({'pred_arm':pred_arm}) #last layer
                outputs.update(pose_out)

        if not args.no_detection:
            orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
            results = postprocessors["bbox"](outputs, orig_target_sizes)
            res = {target["image_id"].item(): output for target, output in zip(targets, results)}
        
    return res



def main(args):
    # Init distributed mode
    dist.init_distributed_mode(args)

    # Update dataset specific configs
    if args.dataset_config is not None:
        # https://stackoverflow.com/a/16878364
        d = vars(args)
        with open(args.dataset_config, "r") as f:
            cfg = json.load(f)
        d.update(cfg)

    #print("git:\n  {}\n".format(utils.get_sha()))

    # Segmentation related
    if args.mask_model != "none":
        args.masks = True
    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"

    print(args)

    device = torch.device(args.device)
    output_dir = Path(args.output_dir)

    # fix the seed for reproducibility
    seed = args.seed + dist.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.use_deterministic_algorithms(False)

    # Build the model
    model, criterion, contrastive_criterion, qa_criterion, weight_dict = build_model(args)
    model.to(device)

    assert (
        criterion is not None or qa_criterion is not None
    ), "Error: should train either detection or question answering (or both)"

    # Get a copy of the model for exponential moving averaged version of the model
    model_ema = deepcopy(model) if args.ema else None
    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print("number of params:", n_parameters)

    # Set up optimizers
    param_dicts = [
        {
            "params": [
                p
                for n, p in model_without_ddp.named_parameters()
                if "backbone" not in n and "text_encoder" not in n and p.requires_grad
            ]
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "text_encoder" in n and p.requires_grad],
            "lr": args.text_encoder_lr,
        },
    ]
    if args.optimizer == "sgd":
        optimizer = torch.optim.SGD(param_dicts, lr=args.lr, momentum=0.9, weight_decay=args.weight_decay)
    elif args.optimizer in ["adam", "adamw"]:
        optimizer = torch.optim.AdamW(param_dicts, lr=args.lr, weight_decay=args.weight_decay)
    else:
        raise RuntimeError(f"Unsupported optimizer {args.optimizer}")

    # Used for loading weights from another model and starting a training from scratch. Especially useful if
    # loading into a model with different functionality.
    if args.load:
        print("loading from", args.load)
        checkpoint = torch.load(args.load, map_location="cpu")
        if "model_ema" in checkpoint:
            model_without_ddp.load_state_dict(checkpoint["model_ema"], strict=False)
        else:
            model_without_ddp.load_state_dict(checkpoint["model"], strict=False)

        if args.ema:
            model_ema = deepcopy(model_without_ddp)

    # Used for resuming training from the checkpoint of a model. Used when training times-out or is pre-empted.
    if args.resume:
        if args.resume.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume, map_location="cpu", check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location="cpu")
        model_without_ddp.load_state_dict(checkpoint["model"])
        if not args.eval and "optimizer" in checkpoint and "epoch" in checkpoint:
            optimizer.load_state_dict(checkpoint["optimizer"])
            args.start_epoch = checkpoint["epoch"] + 1
        if args.ema:
            if "model_ema" not in checkpoint:
                print("WARNING: ema model not found in checkpoint, resetting to current model")
                model_ema = deepcopy(model_without_ddp)
            else:
                model_ema.load_state_dict(checkpoint["model_ema"])


    # Runs only evaluation, by default on the validation set unless --test is passed.
    test_stats = {}
    test_model = model_ema if model_ema is not None else model
    postprocessors = build_postprocessors(args, item.dataset_name)
    print(f"Evaluating {item.dataset_name}")
    
    curr_test_stats = demo(
        model=test_model,
        postprocessors=postprocessors,
        data_loader=item.dataloader,
        device=device,
        args=args,
    )


    return

 

if __name__ == "__main__":
    parser = argparse.ArgumentParser("DETR training and evaluation script", parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    else:
        args.output_dir = './checkpoint1'
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
