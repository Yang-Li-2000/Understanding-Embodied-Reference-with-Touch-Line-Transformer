# Copyright (c): Yang Li and Xiaoxue Chen. Licensed under the Apache License 2.0. All Rights Reserved
# ------------------------------------------------------------------------
# Modified from MDETR (https://github.com/ashkamath/mdetr)
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

import temp_vars
import util.dist as dist
import util.misc as utils
from engine import evaluate, train_one_epoch
from models import build_model
from models.postprocessors import build_postprocessors
from datasets.yourefit import ReferDataset, YouRefItEvaluator
from datasets.coco import make_coco_transforms
from magic_numbers import *
from torch.utils.tensorboard import SummaryWriter
import os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
np.seterr('raise')


def string_to_bool(string):
    if string.lower() == 'false':
        return False
    elif string.lower() == 'true':
        return True
    else:
        raise NotImplementedError()


def get_args_parser():
    parser = argparse.ArgumentParser("Set transformer detector", add_help=False)
    parser.add_argument("--run_name", default="", type=str)

    # Dataset specific
    parser.add_argument("--dataset_config", default=None, required=True)
    parser.add_argument("--do_qa", action="store_true",
                        help="Whether to do question answering")
    parser.add_argument(
        "--predict_final",
        action="store_true",
        help="If true, will predict if a given box is in the actual referred set. Useful for CLEVR-Ref+ only currently.",
    )
    parser.add_argument("--no_detection", action="store_true",
                        help="Whether to train the detector")
    parser.add_argument(
        "--split_qa_heads", action="store_true",
        help="Whether to use a separate head per question type in vqa"
    )
    parser.add_argument(
        "--combine_datasets", nargs="+",
        help="List of datasets to combine for training", default=["flickr"]
    )
    parser.add_argument(
        "--combine_datasets_val", nargs="+",
        help="List of datasets to combine for eval", default=["flickr"]
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
    parser.add_argument("--clip_max_norm", default=0.1, type=float,
                        help="gradient clipping max norm")
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
        choices=(
        "step", "multistep", "linear_with_warmup", "all_linear_with_warmup"),
    )
    parser.add_argument("--ema", action="store_true")
    parser.add_argument("--ema_decay", type=float, default=0.9998)
    parser.add_argument("--fraction_warmup_steps", default=0.01, type=float,
                        help="Fraction of total number of steps")

    # Model parameters
    parser.add_argument(
        "--frozen_weights",
        type=str,
        default=None,
        help="Path to the pretrained model. If set, only the mask head will be trained",
    )

    parser.add_argument(
        "--freeze_text_encoder", action="store_true",
        help="Whether to freeze the weights of the text encoder"
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
    parser.add_argument("--dropout", default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument(
        "--nheads",
        default=8,
        type=int,
        help="Number of attention heads inside the transformer's attentions",
    )
    parser.add_argument("--num_queries", default=20, type=int,
                        help="Number of query slots")
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

    parser.add_argument("--contrastive_loss", action="store_true",
                        help="Whether to add contrastive loss")
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
        "--temperature_NCE", type=float, default=0.07,
        help="Temperature in the  temperature-scaled cross entropy loss"
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

    parser.add_argument("--test", action="store_true",
                        help="Whether to run evaluation on val or test set")
    parser.add_argument("--test_type", type=str, default="test",
                        choices=("testA", "testB", "test"))
    parser.add_argument("--output-dir", default="",
                        help="path where to save, empty for no saving")
    parser.add_argument("--device", default="cuda",
                        help="device to use for training / testing")
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--resume", default="", help="resume from checkpoint")
    parser.add_argument("--load", default="", help="resume from checkpoint")
    parser.add_argument("--start-epoch", default=0, type=int, metavar="N",
                        help="start epoch")
    parser.add_argument("--eval", action="store_true",
                        help="Only run evaluation")
    parser.add_argument("--num_workers", default=5, type=int)

    # Distributed training parameters
    parser.add_argument("--world-size", default=1, type=int,
                        help="number of distributed processes")
    parser.add_argument("--dist-url", default="env://",
                        help="url used to set up distributed training")

    parser.add_argument('--pose', type=string_to_bool, default=True)

    return parser


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
    ARGS_POSE = args.pose
    # print("git:\n  {}\n".format(utils.get_sha()))

    # Segmentation related
    if args.mask_model != "none":
        args.masks = True
    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"

    if REMOVE_LANGUAGE_BY_SETTING_CAPTION_TO_NONE:
        args.contrastive_align_loss = False

    print(args)

    print()
    print()
    print('AMSGRAD:                              ', AMSGRAD)
    print('args.pose:                            ', args.pose)
    if USE_MDETR_PREDICTIONS_AS_GROUNDTRUTHS:
        print('USE_MDETR_PREDICTIONS_AS_GROUNDTRUTHS:',
              USE_MDETR_PREDICTIONS_AS_GROUNDTRUTHS)
    print('REPLACE_ARM_WITH_EYE_TO_FINGERTIP:    ',
          REPLACE_ARM_WITH_EYE_TO_FINGERTIP)
    print('PREDICT_POSE_USING_A_DIFFERENT_MODEL: ', PREDICT_POSE_USING_A_DIFFERENT_MODEL)
    print('ARM_LOSS_COEF:                        ', ARM_LOSS_COEF)
    print('ARM_SCORE_LOSS_COEF:                  ', ARM_SCORE_LOSS_COEF)
    print('ARM_BOX_ALIGN_LOSS_COEF:              ', ARM_BOX_ALIGN_LOSS_COEF)
    print('args.bbox_loss_coef:                  ', args.bbox_loss_coef)
    print('args.giou_loss_coef:                  ', args.giou_loss_coef)
    print('USE_GT__ARM_FOR_ARM_BOX_ALIGN_LOSS:   ',
          USE_GT__ARM_FOR_ARM_BOX_ALIGN_LOSS)
    if ARM_BOX_ALIGN_OFFSET_BY_GT:
        print('ARM_BOX_ALIGN_OFFSET_BY_GT:           ',
              ARM_BOX_ALIGN_OFFSET_BY_GT)
    else:
        print('ARM_BOX_ALIGH_FIXED_OFFSET:           ',
              ARM_BOX_ALIGH_FIXED_OFFSET)
    if DEACTIVATE_EXTRA_TRANSFORMS:
        print('DEACTIVATE_EXTRA_TRANSFORMS:          ',
              DEACTIVATE_EXTRA_TRANSFORMS)
    print('eos_coef:                             ', args.eos_coef)
    if REPLACE_IMAGES_WITH_INPAINT:
        print('REPLACE_IMAGES_WITH_INPAINT:          ',
              REPLACE_IMAGES_WITH_INPAINT)
        print('INPAINT_DIR:                          ', INPAINT_DIR)
    print()
    print('RESERVE_QUERIES_FOR_ARMS:             ', RESERVE_QUERIES_FOR_ARMS)
    print('NUM_RESERVED_QUERIES_FOR_ARMS:        ', NUM_RESERVED_QUERIES_FOR_ARMS)
    print()
    if REPLACE_LANGUAGE_INPUTS:
        print('REPLACE_LANGUAGE_INPUTS:              ', REPLACE_LANGUAGE_INPUTS)
        print('DUMMY_LANGUAGE_INPUT:                 ', DUMMY_LANGUAGE_INPUT)
        print()

    print('COS_SIM_VERTEX:                       ', COS_SIM_VERTEX)
    print()
    print('REPLACE_SENTENCE_WITH_TARGET_WORD:    ', REPLACE_SENTENCE_WITH_TARGET_WORD)
    print()
    print('REMOVE_LANGUAGE_BY_SETTING_CAPTION_TO_NONE: ', REMOVE_LANGUAGE_BY_SETTING_CAPTION_TO_NONE)

    # Initialize tensorboard
    tensorboard_log_dir = 'runs'
    if args.eval:
        start_index = args.load.find('/')
        end_index = args.load.rfind('/')
        experiment_name = args.load[start_index + 1:end_index]
    else:
        start_index = args.output_dir.rfind('/')
        experiment_name = args.output_dir[start_index + 1:]
    writer = SummaryWriter(tensorboard_log_dir + '/' + experiment_name)

    device = torch.device(args.device)
    output_dir = Path(args.output_dir)

    # fix the seed for reproducibility
    seed = args.seed + dist.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.use_deterministic_algorithms(False)

    # Build the model
    model, criterion, contrastive_criterion, qa_criterion, weight_dict = build_model(
        args)
    model.to(device)

    assert (
            criterion is not None or qa_criterion is not None
    ), "Error: should train either detection or question answering (or both)"

    # Get a copy of the model for exponential moving averaged version of the model
    model_ema = deepcopy(model) if args.ema else None
    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.gpu],
                                                          find_unused_parameters=True)
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print("number of params:", n_parameters)

    # Set up optimizers
    param_dicts = [
        {
            "params": [
                p
                for n, p in model_without_ddp.named_parameters()
                if
                "backbone" not in n and "text_encoder" not in n and p.requires_grad
            ]
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if
                       "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if
                       "text_encoder" in n and p.requires_grad],
            "lr": args.text_encoder_lr,
        },
    ]
    if args.optimizer == "sgd":
        optimizer = torch.optim.SGD(param_dicts, lr=args.lr, momentum=0.9,
                                    weight_decay=args.weight_decay)
    elif args.optimizer in ["adam", "adamw"]:
        optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                      weight_decay=args.weight_decay,
                                      amsgrad=AMSGRAD)
    else:
        raise RuntimeError(f"Unsupported optimizer {args.optimizer}")

    # Train dataset
    if len(args.combine_datasets) == 0 and not args.eval:
        raise RuntimeError("Please provide at least one training dataset")

    dataset_train, sampler_train, data_loader_train = None, None, None
    if not args.eval:
        # Deactivate transformations such as random flip and random crop when
        # saving the predictions of mdetr for distillation
        if SAVE_MDETR_PREDICTIONS or DEACTIVATE_EXTRA_TRANSFORMS or TRAIN_EARLY_STOP:
            input_transform = make_coco_transforms('val', False)
        else:
            input_transform = make_coco_transforms('train', False)
        dataset_train = ReferDataset(data_root='.',
                                     split_root='.',
                                     dataset='yourefit',
                                     split='train',
                                     transform=input_transform,
                                     augment=False, args=args)
        if args.distributed:
            sampler_train = DistributedSampler(dataset_train, shuffle=not TRAIN_EARLY_STOP)
        else:
            if TRAIN_EARLY_STOP:
                sampler_train = torch.utils.data.SequentialSampler(dataset_train)
            else:
                sampler_train = torch.utils.data.RandomSampler(dataset_train)

        batch_sampler_train = torch.utils.data.BatchSampler(sampler_train,
                                                            args.batch_size,
                                                            drop_last=DROP_LAST and not CALCULATE_COS_SIM)
        data_loader_train = DataLoader(
            dataset_train,
            batch_sampler=batch_sampler_train,
            collate_fn=partial(utils.collate_fn, False),
            num_workers=args.num_workers,
            persistent_workers=PERSISTENT_WORKERS
        )

    # Val dataset
    if len(args.combine_datasets_val) == 0:
        raise RuntimeError("Please provide at leas one validation dataset")

    Val_all = namedtuple(typename="val_data",
                         field_names=["dataset_name", "dataloader", "base_ds",
                                      "evaluator_list"])

    val_tuples = []
    input_transform = make_coco_transforms('val', False)  # val
    dset = ReferDataset(data_root='./',
                        split_root='./',
                        dataset='yourefit',
                        split='val',
                        transform=input_transform,
                        augment=False, args=args)
    sampler = (
        DistributedSampler(dset,
                           shuffle=False) if args.distributed else torch.utils.data.SequentialSampler(
            dset)
    )
    dataloader = DataLoader(
        dset,
        args.batch_size,
        sampler=sampler,
        drop_last=False,
        collate_fn=partial(utils.collate_fn, False),
        num_workers=args.num_workers,
        persistent_workers=PERSISTENT_WORKERS
    )
    base_ds = None
    val_tuples.append(
        Val_all(dataset_name='yourefit', dataloader=dataloader, base_ds=base_ds,
                evaluator_list=None))
    if args.frozen_weights is not None:
        if args.resume.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume,
                                                            map_location="cpu",
                                                            check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location="cpu")
        if "model_ema" in checkpoint and checkpoint["model_ema"] is not None:
            model_without_ddp.detr.load_state_dict(checkpoint["model_ema"],
                                                   strict=False)
        else:
            model_without_ddp.detr.load_state_dict(checkpoint["model"],
                                                   strict=False)

        if args.ema:
            model_ema = deepcopy(model_without_ddp)

    # Used for loading weights from another model and starting a training from scratch. Especially useful if
    # loading into a model with different functionality.
    if args.load:
        print("loading from", args.load)
        checkpoint = torch.load(args.load, map_location="cpu")
        if "model_ema" in checkpoint:
            model_without_ddp.load_state_dict(checkpoint["model_ema"],
                                              strict=False)
        else:
            model_without_ddp.load_state_dict(checkpoint["model"], strict=False)

        if args.ema:
            model_ema = deepcopy(model_without_ddp)

    # Used for resuming training from the checkpoint of a model. Used when training times-out or is pre-empted.
    if args.resume:
        if args.resume.startswith("https"):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume,
                                                            map_location="cpu",
                                                            check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location="cpu")
        model_without_ddp.load_state_dict(checkpoint["model"])
        if not args.eval and "optimizer" in checkpoint and "epoch" in checkpoint:
            optimizer.load_state_dict(checkpoint["optimizer"])
            args.start_epoch = checkpoint["epoch"] + 1
        if args.ema:
            if "model_ema" not in checkpoint:
                print(
                    "WARNING: ema model not found in checkpoint, resetting to current model")
                model_ema = deepcopy(model_without_ddp)
            else:
                model_ema.load_state_dict(checkpoint["model_ema"])

    def build_evaluator_list(base_ds, dataset_name, dset=None):
        """Helper function to build the list of evaluators for a given dataset"""
        evaluator_list = []
        if args.no_detection:
            return evaluator_list
        iou_types = ["bbox"]
        if args.masks:
            iou_types.append("segm")

        # evaluator_list.append(CocoEvaluator(base_ds, tuple(iou_types), useCats=False))
        if "yourefit" in dataset_name:
            evaluator_list.append(YouRefItEvaluator(dset, ("bbox")))

        return evaluator_list

    # Runs only evaluation, by default on the validation set unless --test is passed.
    if args.eval:
        test_stats = {}
        test_model = model_ema if model_ema is not None else model
        for i, item in enumerate(val_tuples):
            evaluator_list = build_evaluator_list(item.base_ds,
                                                  item.dataset_name, dset)
            postprocessors = build_postprocessors(args, item.dataset_name)
            item = item._replace(evaluator_list=evaluator_list)
            print(f"Evaluating {item.dataset_name}")
            curr_test_stats = evaluate(
                model=test_model,
                criterion=criterion,
                contrastive_criterion=contrastive_criterion,
                qa_criterion=qa_criterion,
                postprocessors=postprocessors,
                weight_dict=weight_dict,
                data_loader=item.dataloader,
                evaluator_list=item.evaluator_list,
                device=device,
                args=args,
            )
            test_stats.update({item.dataset_name + "_" + k: v for k, v in
                               curr_test_stats.items()})

        # Write Precisions to tensorboard
        if len(args.combine_datasets_val) == 1 and args.combine_datasets_val[
            0] == 'yourefit' and dist.get_rank() == 0:

            # Find out epoch number
            start_index = args.load.rfind('/') + len('/checkpoint')
            end_index = args.load.find('.pth')
            epoch_number = args.load[start_index:end_index]
            try:
                epoch_number = int(epoch_number)
            except:
                epoch_number = -1

            # Find out precisions at different IoU thresholds
            precisions = test_stats['yourefit_yourefit']
            p25, p50, p75 = precisions
            # Write precisions to tensorboard
            # writer.add_scalar('Precision/precision_at_0.25', p25, epoch_number)
            # writer.add_scalar('Precision/precision_at_0.50', p50, epoch_number)
            # writer.add_scalar('Precision/precision_at_0.75', p75, epoch_number)

            # Find out  losses
            # total_loss = test_stats['yourefit_loss']
            #
            # unscaled_ce_loss = test_stats['yourefit_loss_ce_unscaled']
            # unscaled_giou_loss = test_stats['yourefit_loss_giou_unscaled']
            # unscaled_box_loss = test_stats['yourefit_loss_bbox_unscaled']
            # unscaled_contrastive_align_loss =  test_stats['yourefit_loss_contrastive_align_unscaled']

            if dist.get_world_size() > 1:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(
                        model.module.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            else:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(model.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            # # unscaled_pose_loss = test_stats['yourefit_pose_loss_' + str(pose_decoder_last_layer_index) + '_unscaled']
            # unscaled_arm_loss = test_stats['yourefit_arm_loss_' + str(
            #     pose_decoder_last_layer_index) + '_unscaled']
            # unscaled_arm_score_loss = test_stats[
            #     'yourefit_arm_score_loss_' + str(
            #         pose_decoder_last_layer_index) + '_unscaled']

            # Write losses to tensorboard
            # writer.add_scalar('Loss/valid_total', total_loss, epoch_number)
            #
            # writer.add_scalar('Loss_valid_unscaled/ce', unscaled_ce_loss, epoch_number)
            # writer.add_scalar('Loss_valid_unscaled/giou', unscaled_giou_loss, epoch_number)
            # writer.add_scalar('Loss_valid_unscaled/box', unscaled_box_loss, epoch_number)
            # writer.add_scalar('Loss_valid_unscaled/contrastive_align', unscaled_contrastive_align_loss, epoch_number)
            #
            # writer.add_scalar('Loss_valid_unscaled/arm', unscaled_arm_loss, epoch_number)
            # writer.add_scalar('Loss_valid_unscaled/arm_score', unscaled_arm_score_loss, epoch_number)

        log_stats = {
            **{f"test_{k}": v for k, v in test_stats.items()},
            "n_parameters": n_parameters,
        }
        print(log_stats)
        return

    # Runs training and evaluates after every --eval_skip epochs
    print("Start training")
    start_time = time.time()
    best_metric = 0.0
    for epoch in range(args.start_epoch, args.epochs):
        print(f"Starting epoch {epoch}")
        if args.distributed:
            sampler_train.set_epoch(epoch)
        train_stats = train_one_epoch(
            model=model,
            criterion=criterion,
            contrastive_criterion=contrastive_criterion,
            qa_criterion=qa_criterion,
            data_loader=data_loader_train,
            weight_dict=weight_dict,
            optimizer=optimizer,
            device=device,
            epoch=epoch,
            args=args,
            max_norm=args.clip_max_norm,
            model_ema=model_ema,
        )

        # Write train stats to tensorboard
        if dist.get_rank() == 0:

            # Find out epoch number
            epoch_number = epoch

            # Find out lr
            lr = train_stats['lr']
            writer.add_scalar('Misc_train/lr', lr, epoch_number)
            writer.add_scalar('Misc_train/ARM_LOSS_COEF', ARM_LOSS_COEF,
                              epoch_number)
            writer.add_scalar('Misc_train/ARM_SCORE_LOSS_COEF',
                              ARM_SCORE_LOSS_COEF, epoch_number)
            writer.add_scalar('Misc_train/ARM_BOX_ALIGN_LOSS_COEF',
                              ARM_BOX_ALIGN_LOSS_COEF, epoch_number)
            if ARM_BOX_ALIGN_OFFSET_BY_GT:
                writer.add_scalar('Misc_train/ARM_BOX_ALIGH_FIXED_OFFSET',
                                  ARM_BOX_ALIGH_FIXED_OFFSET, epoch_number)
            else:
                writer.add_scalar('Misc_train/ARM_BOX_ALIGH_FIXED_OFFSET', -1,
                                  epoch_number)
            writer.add_scalar('Misc_train/eos_coef', args.eos_coef,
                              epoch_number)

            # Find out losses
            total_loss = train_stats['loss']

            unscaled_ce_loss = train_stats['loss_ce_unscaled']
            unscaled_giou_loss = train_stats['loss_giou_unscaled']
            unscaled_box_loss = train_stats['loss_bbox_unscaled']
            if 'loss_contrastive_align_unscaled' in train_stats.keys():
                unscaled_contrastive_align_loss = train_stats[
                    'loss_contrastive_align_unscaled']
            else:
                unscaled_contrastive_align_loss = 0.0

            if dist.get_world_size() > 1:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(
                        model.module.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            else:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(model.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            # unscaled_pose_loss = train_stats['pose_loss_' + str(pose_decoder_last_layer_index) + '_unscaled']
            if ARGS_POSE:
                unscaled_arm_loss = train_stats[
                    'arm_loss_' + str(
                        pose_decoder_last_layer_index) + '_unscaled']
                unscaled_arm_score_loss = train_stats['arm_score_loss_' + str(
                    pose_decoder_last_layer_index) + '_unscaled']
                unscaled_arm_box_align_loss = train_stats[
                    'arm_box_aligned_loss_unscaled']
            else:
                unscaled_arm_loss = -1
                unscaled_arm_score_loss = -1
                unscaled_arm_box_align_loss = -1

            # Write losses to tensorboard
            writer.add_scalar('Loss/train_total', total_loss, epoch_number)

            writer.add_scalar('Loss_train_unscaled/ce', unscaled_ce_loss,
                              epoch_number)
            writer.add_scalar('Loss_train_unscaled/giou', unscaled_giou_loss,
                              epoch_number)
            writer.add_scalar('Loss_train_unscaled/box', unscaled_box_loss,
                              epoch_number)
            writer.add_scalar('Loss_train_unscaled/contrastive_align',
                              unscaled_contrastive_align_loss, epoch_number)

            writer.add_scalar('Loss_train_unscaled/arm', unscaled_arm_loss,
                              epoch_number)
            writer.add_scalar('Loss_train_unscaled/arm_score',
                              unscaled_arm_score_loss, epoch_number)
            writer.add_scalar('Loss_train_unscaled/arm_box_align',
                              unscaled_arm_box_align_loss, epoch_number)

        if args.output_dir:
            checkpoint_paths = [output_dir / "checkpoint.pth"]
            # extra checkpoint before LR drop and every 2 epochs
            if (epoch + 1) % args.lr_drop == 0 or (
                    epoch + 1) % CHECKPOINT_FREQUENCY == 0:
                checkpoint_paths.append(
                    output_dir / f"checkpoint{epoch:04}.pth")
            for checkpoint_path in checkpoint_paths:
                dist.save_on_master(
                    {
                        "model": model_without_ddp.state_dict(),
                        "model_ema": model_ema.state_dict() if args.ema else None,
                        "optimizer": optimizer.state_dict(),
                        "epoch": epoch,
                        "args": args,
                    },
                    checkpoint_path,
                )

        if epoch % args.eval_skip == 0:
            test_stats = {}
            test_model = model_ema if model_ema is not None else model
            for i, item in enumerate(val_tuples):
                evaluator_list = build_evaluator_list(item.base_ds,
                                                      item.dataset_name, dset)
                item = item._replace(evaluator_list=evaluator_list)
                postprocessors = build_postprocessors(args, item.dataset_name)
                print(f"Evaluating {item.dataset_name}")
                curr_test_stats = evaluate(
                    model=test_model,
                    criterion=criterion,
                    contrastive_criterion=contrastive_criterion,
                    qa_criterion=qa_criterion,
                    postprocessors=postprocessors,
                    weight_dict=weight_dict,
                    data_loader=item.dataloader,
                    evaluator_list=item.evaluator_list,
                    device=device,
                    args=args,
                )
                test_stats.update({item.dataset_name + "_" + k: v for k, v in
                                   curr_test_stats.items()})
        else:
            test_stats = {}

        # Write test stats to tensorboard
        if len(test_stats) > 0 \
                and dist.get_rank() == 0 \
                and len(args.combine_datasets_val) == 1 \
                and args.combine_datasets_val[0] == 'yourefit':

            # Find out epoch number
            epoch_number = epoch
            epoch_number = int(epoch_number)

            # Find out precisions at different IoU thresholds
            precisions = test_stats['yourefit_yourefit']
            p25, p50, p75 = precisions
            # Write precisions to tensorboard
            writer.add_scalar('Precision/precision_at_0.25', p25, epoch_number)
            writer.add_scalar('Precision/precision_at_0.50', p50, epoch_number)
            writer.add_scalar('Precision/precision_at_0.75', p75, epoch_number)

            # Find out  losses
            total_loss = test_stats['yourefit_loss']

            unscaled_ce_loss = test_stats['yourefit_loss_ce_unscaled']
            unscaled_giou_loss = test_stats['yourefit_loss_giou_unscaled']
            unscaled_box_loss = test_stats['yourefit_loss_bbox_unscaled']
            if 'yourefit_loss_contrastive_align_unscaled' in test_stats.keys():
                unscaled_contrastive_align_loss = test_stats['yourefit_loss_contrastive_align_unscaled']
            else:
                unscaled_contrastive_align_loss = 0.0


            if dist.get_world_size() > 1:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(
                        model.module.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            else:
                if PREDICT_POSE_USING_A_DIFFERENT_MODEL:
                    pose_decoder_last_layer_index = len(model.pose_decoder) - 1
                else:
                    pose_decoder_last_layer_index = POSE_MLP_NUM_LAYERS - 1
            # unscaled_pose_loss = test_stats['yourefit_pose_loss_' + str(pose_decoder_last_layer_index) + '_unscaled']
            if ARGS_POSE:
                unscaled_arm_loss = test_stats['yourefit_arm_loss_' + str(
                    pose_decoder_last_layer_index) + '_unscaled']
                unscaled_arm_score_loss = test_stats[
                    'yourefit_arm_score_loss_' + str(
                        pose_decoder_last_layer_index) + '_unscaled']
                unscaled_arm_box_align_loss = train_stats[
                    'arm_box_aligned_loss_unscaled']
            else:
                unscaled_arm_loss = -1
                unscaled_arm_score_loss = -1
                unscaled_arm_box_align_loss = -1

            # Write losses to tensorboard
            writer.add_scalar('Loss/valid_total', total_loss, epoch_number)

            writer.add_scalar('Loss_valid_unscaled/ce', unscaled_ce_loss,
                              epoch_number)
            writer.add_scalar('Loss_valid_unscaled/giou', unscaled_giou_loss,
                              epoch_number)
            writer.add_scalar('Loss_valid_unscaled/box', unscaled_box_loss,
                              epoch_number)
            writer.add_scalar('Loss_valid_unscaled/contrastive_align',
                              unscaled_contrastive_align_loss, epoch_number)

            writer.add_scalar('Loss_valid_unscaled/arm', unscaled_arm_loss,
                              epoch_number)
            writer.add_scalar('Loss_valid_unscaled/arm_score',
                              unscaled_arm_score_loss, epoch_number)
            writer.add_scalar('Loss_valid_unscaled/arm_box_align',
                              unscaled_arm_box_align_loss, epoch_number)

        log_stats = {
            **{f"train_{k}": v for k, v in train_stats.items()},
            **{f"test_{k}": v for k, v in test_stats.items()},
            "epoch": epoch,
            "n_parameters": n_parameters,
        }

        if args.output_dir and dist.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")

        if epoch % args.eval_skip == 0:
            Save_Best_Checkpoint = True
            if args.do_qa:
                metric = test_stats["gqa_accuracy_answer_total_unscaled"]
            else:
                # get p75 for yourefit
                temp = [v[-1] for k, v in test_stats.items() if "yourefit_yourefit" in k]
                if len(temp) > 0:
                    metric = np.mean(temp)
                else:
                    metric = temp
                    Save_Best_Checkpoint = False

            if Save_Best_Checkpoint and args.output_dir and metric > temp_vars.max_p75:
                temp_vars.max_p75 = metric
                checkpoint_paths = [output_dir / ("BEST_checkpoint_since" + str(args.start_epoch) + ".pth")]
                # extra checkpoint before LR drop and every 100 epochs
                for checkpoint_path in checkpoint_paths:
                    dist.save_on_master(
                        {
                            "model": model_without_ddp.state_dict(),
                            "model_ema": model_ema.state_dict() if args.ema else None,
                            "optimizer": optimizer.state_dict(),
                            "epoch": epoch,
                            "args": args,
                        },
                        checkpoint_path,
                    )

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print("Training time {}".format(total_time_str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser("DETR training and evaluation script",
                                     parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    else:
        args.output_dir = './checkpoint1'
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
