# Copyright (c) Meta Platforms, Inc. and affiliates.

# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import argparse
import datetime
import numpy as np
import time
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import json
import os
import torch.nn.functional as F
from pathlib import Path
from convnext import *
import cswin
#from convnextv2 import convnextv2_tiny
from timm.data.mixup import Mixup
from timm1.models import create_model as create_model1
from timm1.models import resnet50,mobilenetv3_large_100
from timm.models import create_model
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from model_sema import ModelEma
from optim_factory import create_optimizer, LayerDecayValueAssigner
from datasets import build_dataset
from engine_kd import train_one_epoch, evaluate
from utils import NativeScalerWithGradNormCount as NativeScaler
from sparse_core import Masking, CosineDecay
import warnings
from convnext import *
import torchvision
warnings.filterwarnings("ignore")
import utils
import models.SLaK


class MGDLoss(nn.Module):

    """PyTorch version of `Masked Generative Distillation`
   
    Args:
        student_channels(int): Number of channels in the student's feature map.
        teacher_channels(int): Number of channels in the teacher's feature map. 
        name (str): the loss name of the layer
        alpha_mgd (float, optional): Weight of dis_loss. Defaults to 0.00007
        lambda_mgd (float, optional): masked ratio. Defaults to 0.5
    """
    def __init__(self,
                 student_channels,
                 teacher_channels,
                 alpha_mgd=0.00007,
                 lambda_mgd=0.5,
                 ):
        super(MGDLoss, self).__init__()
        self.alpha_mgd = alpha_mgd
        self.lambda_mgd = lambda_mgd
    
        if student_channels != teacher_channels:
            self.align = nn.Conv2d(student_channels, teacher_channels, kernel_size=1, stride=1, padding=0)
        else:
            self.align = None

        self.generation = nn.Sequential(
            nn.Conv2d(teacher_channels, teacher_channels, kernel_size=3, padding=1),
            nn.ReLU(inplace=True), 
            nn.Conv2d(teacher_channels, teacher_channels, kernel_size=3, padding=1))


    def forward(self,
                preds_S,
                preds_T):
        """Forward function.
        Args:
            preds_S(Tensor): Bs*C*H*W, student's feature map
            preds_T(Tensor): Bs*C*H*W, teacher's feature map
        """
        N, C, H, W = preds_T.shape
        if  preds_S.shape[-2:] != preds_T.shape[-2:]:
            preds_S=F.interpolate(preds_S,(H,W),mode='bilinear')

        if self.align is not None:
            preds_S = self.align(preds_S)
    
        loss = self.get_dis_loss(preds_S, preds_T)*self.alpha_mgd
            
        return loss

    def get_dis_loss(self, preds_S, preds_T):
        loss_mse = nn.MSELoss(reduction='sum')
        N, C, H, W = preds_T.shape

        device = preds_S.device
        mat = torch.rand((N,C,1,1)).to(device)
        # mat = torch.rand((N,1,H,W)).to(device)
        mat = torch.where(mat < self.lambda_mgd, 0, 1).to(device)

        masked_fea = torch.mul(preds_S, mat)
        new_fea = self.generation(masked_fea)
        #if new_fea.shape[-1]!=preds_T.shape[-1]:
        #    new_fea=F.interpolate(new_fea,(H,W),mode='bilinear')
        dis_loss = loss_mse(new_fea, preds_T)/N

        return dis_loss


def kernel_type(strings):
    strings = strings.replace("(", "").replace(")", "")
    mapped_int = map(int, strings.split(","))
    return [tuple(mapped_int[:-1]), mapped_int[-1]]

def loss_kd(preds, labels, teacher_preds):
    T = 1
    alpha = 0.9
    loss = F.kl_div(F.log_softmax(preds / T, dim=1), F.softmax(teacher_preds / T, dim=1),
                    reduction='batchmean') * T * T * alpha + F.cross_entropy(preds, labels) * (1. - alpha)
    return loss

def str2bool(v):
    """
    Converts string to bool type; enables command line 
    arguments in the format of '--arg1 true --arg2 false'
    """
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def get_args_parser():
    parser = argparse.ArgumentParser('SLaK training and evaluation script for image classification', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Per GPU batch size')
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--update_freq', default=1, type=int,
                        help='gradient accumulation steps')

    # Model parameters
    parser.add_argument('--model', default='SLaK_tiny', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--model_s', default='SLaK_small', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--drop_path', type=float, default=0, metavar='PCT',
                        help='Drop path rate (default: 0.0)')
    parser.add_argument('--input_size', default=224, type=int,
                        help='image input size')
    parser.add_argument('--layer_scale_init_value', default=1e-6, type=float,
                        help="Layer scale initial values")

    # EMA related parameters
    parser.add_argument('--model_ema', type=str2bool, default=False)
    parser.add_argument('--model_ema_decay', type=float, default=0.9999, help='')
    parser.add_argument('--model_ema_force_cpu', type=str2bool, default=False, help='')
    parser.add_argument('--model_ema_eval', type=str2bool, default=False, help='Using ema to eval during training.')

    # Optimization parameters
    parser.add_argument('--opt', default='adamw', type=str, metavar='OPTIMIZER',
                        help='Optimizer (default: "adamw"')
    parser.add_argument('--opt_eps', default=1e-8, type=float, metavar='EPSILON',
                        help='Optimizer Epsilon (default: 1e-8)')
    parser.add_argument('--opt_betas', default=None, type=float, nargs='+', metavar='BETA',
                        help='Optimizer Betas (default: None, use opt default)')
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')
    parser.add_argument('--weight_decay_end', type=float, default=None, help="""Final value of the
        weight decay. We use a cosine schedule for WD and using a larger decay by
        the end of training improves performance for ViTs.""")

    parser.add_argument('--lr', type=float, default=4e-3, metavar='LR',
                        help='learning rate (default: 4e-3), with total batch size 4096')
    parser.add_argument('--layer_decay', type=float, default=1.0)
    parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0 (1e-6)')
    parser.add_argument('--warmup_epochs', type=int, default=20, metavar='N',
                        help='epochs to warmup LR, if scheduler supports')
    parser.add_argument('--warmup_steps', type=int, default=-1, metavar='N',
                        help='num of steps to warmup LR, will overload warmup_epochs if set > 0')

    # Augmentation parameters
    parser.add_argument('--color_jitter', type=float, default=0.4, metavar='PCT',
                        help='Color jitter factor (default: 0.4)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')
    parser.add_argument('--train_interpolation', type=str, default='bicubic',
                        help='Training interpolation (random, bilinear, bicubic default: "bicubic")')

    # Evaluation parameters
    parser.add_argument('--crop_pct', type=float, default=None)

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', type=str2bool, default=False,
                        help='Do not random erase first (clean) augmentation split')
    parser.add_argument('--T', type=float, default=1.0,help='tempature')
    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0.8,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=1.0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # * Finetuning params
    parser.add_argument('--finetune', default='',
                        help='finetune from checkpoint')
    parser.add_argument('--head_init_scale', default=1.0, type=float,
                        help='classifier head initial scale, typically adjusted in fine-tuning')
    parser.add_argument('--model_key', default='model|module', type=str,
                        help='which key to load from saved state dict, usually model or model_ema')
    parser.add_argument('--model_prefix', default='', type=str)

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--eval_data_path', default=None, type=str,
                        help='dataset path for evaluation')
    parser.add_argument('--nb_classes', default=1000, type=int,
                        help='number of the classification types')
    parser.add_argument('--imagenet_default_mean_and_std', type=str2bool, default=True)
    parser.add_argument('--data_set', default='IMNET', choices=['CIFAR', 'IMNET', 'image_folder'],
                        type=str, help='ImageNet dataset path')
    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default=None,
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)

    parser.add_argument('--distill_type', default='KD', type=str)
    parser.add_argument('--FDLoss_type', default='smoothL1', type=str,choices=['smoothL1','MSE'])
    parser.add_argument('--lr_fd', default=1.0, type=float)
    parser.add_argument('--alpha_mgd', default=7e-5, type=float)
    parser.add_argument('--alpha', default=0.1, type=float)
    parser.add_argument('--lambda_mgd', default=0.5, type=float)
    

    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--auto_resume', type=str2bool, default=True)
    parser.add_argument('--save_ckpt', type=str2bool, default=True)
    parser.add_argument('--save_ckpt_freq', default=10, type=int)
    parser.add_argument('--save_ckpt_num', default=3, type=int)

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', type=str2bool, default=False,
                        help='Perform evaluation only')
    parser.add_argument('--dist_eval', type=str2bool, default=True,
                        help='Enabling distributed evaluation')
    parser.add_argument('--disable_eval', type=str2bool, default=False,
                        help='Disabling evaluation during training')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', type=str2bool, default=True,
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', type=str2bool, default=False)
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    parser.add_argument('--use_amp', type=str2bool, default=False, 
                        help="Use PyTorch's AMP (Automatic Mixed Precision) or not")

    # Weights and Biases arguments
    parser.add_argument('--enable_wandb', type=str2bool, default=False,
                        help="enable logging to Weights and Biases")
    parser.add_argument('--project', default='SLaK', type=str,
                        help="The name of the W&B project where you're sending the new run.")
    parser.add_argument('--wandb_ckpt', type=str2bool, default=False,
                        help="Save model checkpoints as W&B Artifacts.")
    parser.add_argument('--hard', action='store_true', help='hard loss.')
    parser.add_argument('--distill_resume', action='store_true', help='resume for student model.')
    parser.add_argument('--feature_n', default=1, type=int)
    parser.add_argument('--target_Lnorm', action='store_true', help='layer norm for target.')
    parser.add_argument('--flag', action='store_true', help='flag')

    # large kernel
    parser.add_argument('--Decom', type=str2bool, default=False, help='Enabling kernel decomposition')
    parser.add_argument('--width_factor', type=float, default=1.0, help='set the width factor of the model')
    parser.add_argument('--sparse', action='store_true', help='Enable sparse model. Default: False.')
    parser.add_argument('--kernel_size', nargs="*", type=int, default = [51,49,47,13,5], help='kernel size of conv [stage1, stage2, stage3, stage4, N]')
    parser.add_argument('--growth', type=str, default='random', help='Growth mode. Choose from: momentum, random, gradient.')
    parser.add_argument('--prune', type=str, default='magnitude', help='Prune mode / pruning mode. Choose from: magnitude, SET.')
    parser.add_argument('--redistribution', type=str, default='none', help='Redistribution mode. Choose from: momentum, magnitude, nonzeros, or none.')
    parser.add_argument('--prune_rate', type=float, default=0.3, help='The pruning rate / prune rate.')
    parser.add_argument('--sparsity', type=float, default=0.4, help='The sparsity of the overall sparse network.')
    parser.add_argument('--verbose', action='store_true', help='Prints verbose status of pruning/growth algorithms.')
    parser.add_argument('--fix', action='store_true', help='Fix sparse model during training i.e., no weight adaptation.')
    parser.add_argument('--sparse_init', type=str, default='snip', help='layer-wise sparsity ratio')
    parser.add_argument('-u', '--update-frequency', type=int, default=100, metavar='N', help='how many iterations to adapt weights')
    parser.add_argument('--only-L', action='store_true', help='only sparsify large kernels.')
    parser.add_argument('--bn', type=str2bool, default=True, help='add batch norm layer after each path')


    return parser


# swin_kernel_dict={0:7,1:7,2:14,3:28}
# slak_kernel_dict={0:7,1:14,2:28,3:56}
# vit_kernel_dict={0:14}
# vit_dict={3:768}
# convnext_kernel_dict={0:7,1:14,2:28,3:56}
# swin_dict={0:192,1:384,2:768,3:768}
# convnext_dict={0:96,1:192,2:384,3:768}
# resnet_dict={0:256,1:512,2:1024,3:2048}
# slak_dict={0:124,1:249,2:499,3:998}

students_dict2={"resnet50":2048,"convnextv2":768,"SLaK_tiny":768}
teachers_dict2={"SLaK_tiny":998,"convnext":768,"SLaK_small":998,"swin":768,"vit":768}

def main(args):
    utils.init_distributed_mode(args)
    print(args)
    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    cudnn.benchmark = True

    dataset_train, args.nb_classes = build_dataset(is_train=True, args=args)
    if args.disable_eval:
        args.dist_eval = False
        dataset_val = None
    else:
        dataset_val, _ = build_dataset(is_train=False, args=args)

    num_tasks = utils.get_world_size()
    global_rank = utils.get_rank()

    sampler_train = torch.utils.data.DistributedSampler(
        dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed,
    )
    print("Sampler_train = %s" % str(sampler_train))
    if args.dist_eval:
        if len(dataset_val) % num_tasks != 0:
            print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                    'This will slightly alter validation results as extra duplicate entries are added to achieve '
                    'equal num of samples per-process.')
        sampler_val = torch.utils.data.DistributedSampler(
            dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=False)
    else:
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    if global_rank == 0 and args.log_dir is not None:
        os.makedirs(args.log_dir, exist_ok=True)
        log_writer = utils.TensorboardLogger(log_dir=args.log_dir)
    else:
        log_writer = None

    if global_rank == 0 and args.enable_wandb:
        wandb_logger = utils.WandbLogger(args)
    else:
        wandb_logger = None

    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, sampler=sampler_train,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=True,
    )

    if dataset_val is not None:
        data_loader_val = torch.utils.data.DataLoader(
            dataset_val, sampler=sampler_val,
            batch_size=int(1.5 * args.batch_size),
            num_workers=args.num_workers,
            pin_memory=args.pin_mem,
            drop_last=False
        )
    else:
        data_loader_val = None

    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None

    if mixup_active:
        print("Mixup is activated!")
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.nb_classes)
    #vit_large_patch16_224
    if args.model=='convnext':
        model=convnext_tiny(num_classes=args.nb_classes,drop_path_rate=args.drop_path,layer_scale_init_value=args.layer_scale_init_value,head_init_scale=args.head_init_scale)
    elif args.model=='convnext21k':
        model=convnext_tiny(num_classes=args.nb_classes,drop_path_rate=args.drop_path,layer_scale_init_value=args.layer_scale_init_value,head_init_scale=args.head_init_scale)
    elif args.model=='vit':
        model=create_model1('vit_small_patch16_224',pretrained=True)
    elif args.model=='vit21klarge':
        model=create_model1('vit_large_patch16_224',pretrained=True)
    elif args.model=='vit21k':
        model=create_model1('vit_base_patch16_224',pretrained=True)        
    elif args.model=='vitdeit':
        model=create_model1('vit_deit_small_patch16_224',pretrained=True)
    elif args.model=='vitbase':
        model=create_model1('vit_base_patch16_224',pretrained=True)
    elif args.model=='swin':
        model=create_model1('swin_tiny_patch4_window7_224',pretrained=True)
    elif args.model=='efficientnet':
        model=create_model1('tf_efficientnet_b3_ns',pretrained=True)
    elif args.model=='resnet50d':
        model=create_model1('resnet50d',pretrained=True)
    elif args.model=='cswin':
        model=cswin.CSWin_64_12211_tiny_224()
        ck=torch.load("./checkpoints/cswin_tiny_224.pth",map_location='cpu')['state_dict_ema']
        model.load_state_dict(ck)
    else:
        model = create_model(
            args.model,
            pretrained=False,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            layer_scale_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale,
            kernel_size=args.kernel_size,
            width_factor=args.width_factor,
            Decom=args.Decom,
            bn = args.bn,
            )
        print("model",args.model,"Decom",args.Decom)
    if args.model_s=='resnet50':
        model_convnext=resnet50(args=args)
    elif args.model_s=='mobilenet':
        model_convnext=mobilenetv3_large_100(args=args)
    elif args.model_s=='convnextv2':
        #pass
        model_convnext = create_model(
            'SLaK_tiny',
            pretrained=False,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            layer_scale_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale,
            kernel_size=[7,7,7,7,100],
            width_factor=1.0,
            Decom=False,
            bn = False,
            args=args,
            gru=True,
            flag=args.flag
            )
        print(model_convnext)
    elif args.model_s=='convnextv2_small':
        model_convnext = create_model(
            'SLaK_small',
            pretrained=False,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            layer_scale_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale,
            kernel_size=[7,7,7,7,100],
            width_factor=1.0,
            Decom=False,
            bn = False,
            args=args,
            gru=True,
            flag=args.flag
            )
        print(model_convnext)
    elif args.model_s=='convnextoriginal':
        if args.finetune:
            model_convnext=convnext_tiny(num_classes=21841,pretrained=True,in_22k=True)
        else:    
            model_convnext=convnext_tiny(num_classes=args.nb_classes,drop_path_rate=args.drop_path,layer_scale_init_value=args.layer_scale_init_value,head_init_scale=args.head_init_scale)
    else:
        model_convnext = create_model(
            args.model_s,
            pretrained=False,
            num_classes=args.nb_classes,
            drop_path_rate=args.drop_path,
            layer_scale_init_value=args.layer_scale_init_value,
            head_init_scale=args.head_init_scale,
            kernel_size=[7,7,7,7,100],
            width_factor=1.0,
            Decom=False,
            bn = args.bn,
            args=args,
            flag=args.flag
            )
    print ("model_convnext",model_convnext)
    # model_convnext = convnext_small(
    #     num_classes=args.nb_classes, 
    #     drop_path_rate=args.drop_path,
    #     layer_scale_init_value=args.layer_scale_init_value,
    #     head_init_scale=args.head_init_scale,
    #     )

    if args.finetune:
        if args.model_s=="convnextoriginal":
            model_convnext.head=nn.Linear(768,1000)
        else:
            assert False
        # if args.finetune.startswith('https'):
        #     checkpoint = torch.hub.load_state_dict_from_url(
        #         args.finetune, map_location='cpu', check_hash=True)
        # else:
        #     checkpoint = torch.load(args.finetune, map_location='cpu')

        # print("Load ckpt from %s" % args.finetune)
        
        # checkpoint_model = None
        # for model_key in args.model_key.split('|'):
        #     if model_key in checkpoint:
        #         checkpoint_model = checkpoint[model_key]
        #         print("Load state_dict by model_key = %s" % model_key)
        #         break
        # if checkpoint_model is None:
        #     checkpoint_model = checkpoint
        # state_dict = model.state_dict()
        # for k in ['head.weight', 'head.bias']:
        #     if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
        #         print(f"Removing key {k} from pretrained checkpoint")
        #         del checkpoint_model[k]
        #model_convnext.load_state_dict(checkpoint['model'])
        #utils.load_state_dict(model_convnext, checkpoint, prefix=args.model_prefix)
        print("student model loaded!")
        #assert False
    model.to(device)
    model_convnext.to(device)
     
    
    model_ema = None
    if args.model_ema:
        # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
        model_ema = ModelEma(
            model_convnext,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume='')
        model_ema_t = ModelEma(
            model,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume='')

        print("Using EMA with decay = %.8f" % args.model_ema_decay)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("Model = %s" % str(model_without_ddp))
    print('number of params:', n_parameters)

    total_batch_size = args.batch_size * args.update_freq * utils.get_world_size()
    num_training_steps_per_epoch = len(dataset_train) // total_batch_size

    print("LR = %.8f" % args.lr)
    print("Batch size = %d" % total_batch_size)
    print("Update frequent = %d" % args.update_freq)
    print("Number of training examples = %d" % len(dataset_train))
    print("Number of training steps per epoch = %d" % num_training_steps_per_epoch)

    if args.layer_decay < 1.0 or args.layer_decay > 1.0:
        num_layers = 12 # SLak layers divided into 12 parts, each with a different decayed lr value.
        assert args.model in ['SLaK_tiny', 'SLaK_small', 'SLaK_base', 'SLaK_large'], \
             "Layer Decay impl only supports SLaK_small/base/large/xlarge"
        assigner = LayerDecayValueAssigner(list(args.layer_decay ** (num_layers + 1 - i) for i in range(num_layers + 2)))
    else:
        assigner = None
    import os
    #args.gpu=torch.distributed.get_rank()
    if assigner is not None:
        print("Assigned values = %s" % str(assigner.values))

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=False)
        model_without_ddp = model.module
        
        model_convnext = torch.nn.parallel.DistributedDataParallel(model_convnext, device_ids=[args.gpu], find_unused_parameters=False)
        model_convnext_without_ddp = model_convnext.module
    
    if 'MGD' in args.distill_type:
        MGD_loss=MGDLoss(students_dict2[args.model_s],teachers_dict2[args.model],args.alpha_mgd,args.lambda_mgd)
        MGD_loss.to(device)
        MGD_P=MGD_loss.parameters()
    else:
        MGD_P=None
        MGD_loss=None

    optimizer = create_optimizer(
        args, model_convnext_without_ddp, skip_list=None,
        get_num_layer=assigner.get_layer_id if assigner is not None else None, 
        get_layer_scale=assigner.get_scale if assigner is not None else None)


    loss_scaler = NativeScaler() # if args.use_amp is False, this won't be used

    print("Use Cosine LR scheduler")
    lr_schedule_values = utils.cosine_scheduler(
        args.lr, args.min_lr, args.epochs, num_training_steps_per_epoch,
        warmup_epochs=args.warmup_epochs, warmup_steps=args.warmup_steps,
    )

    if args.weight_decay_end is None:
        args.weight_decay_end = args.weight_decay
    wd_schedule_values = utils.cosine_scheduler(
        args.weight_decay, args.weight_decay_end, args.epochs, num_training_steps_per_epoch)
    print("Max WD = %.7f, Min WD = %.7f" % (max(wd_schedule_values), min(wd_schedule_values)))

    if mixup_fn is not None:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = torch.nn.CrossEntropyLoss()

    print("criterion = %s" % str(criterion))

    if args.model!='vit' and args.model!='resnet50d' and args.model!='swin' and args.model!='vitbase' and args.model!='vitdeit' and args.model!='cswin' and args.model!='efficientnet' and args.model!='vit21k' and args.model!='vit21klarge': 
        print("model",args.model,"Decom",args.Decom)
        utils.auto_load_model1(
            args=args, model=model, model_without_ddp=model_without_ddp,
            optimizer=optimizer, loss_scaler=loss_scaler, model_ema=model_ema_t)
    if args.distill_resume:
        utils.auto_load_model(
            args=args, model=model_convnext, model_without_ddp=model_convnext_without_ddp,
            optimizer=optimizer, loss_scaler=loss_scaler, model_ema=model_ema)
        print("student model loaded at:",args.start_epoch)
    #if args.eval:

    for name, weight in model.named_parameters():
        print(f"{name} density is {(weight != 0.0).sum().item()/weight.numel()}")
    test_stats = evaluate(data_loader_val, model, device, use_amp=args.use_amp)
    test_stats_student = evaluate(data_loader_val, model_convnext, device, use_amp=args.use_amp)
    #return
    print(f"Accuracy of the network on {len(dataset_val)} test images: {test_stats['acc1']:.5f}%","Start accuracy for student model:",test_stats_student)

        #return

    # num_training_steps_per_epoch is the number of the actual training steps
    mask=None
    if args.sparse:
        decay = CosineDecay(args.prune_rate, int(num_training_steps_per_epoch*args.epochs), init_step= int(num_training_steps_per_epoch)*(args.start_epoch))
        mask = Masking(optimizer, train_loader=data_loader_train, prune_mode=args.prune, prune_rate_decay=decay, growth_mode=args.growth, redistribution_mode=args.redistribution, args=args)
        mask.add_module(model)

    max_accuracy = 0.0
    if args.model_ema and args.model_ema_eval:
        max_accuracy_ema = 0.0

    para_count = 0
    for name, para in model.named_parameters():
        para_count += (para!=0).sum().item()
    print(f"Total number of parameters are {para_count}")

    print("Start training for %d epochs" % args.epochs)
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)
        if log_writer is not None:
            log_writer.set_step(epoch * num_training_steps_per_epoch * args.update_freq)
        if wandb_logger:
            wandb_logger.set_steps()
        train_stats = train_one_epoch(
            model,model_convnext, criterion, data_loader_train, optimizer,
            device, epoch, loss_scaler, args.clip_grad, model_ema, mixup_fn,
            log_writer=log_writer, wandb_logger=wandb_logger, start_steps=epoch * num_training_steps_per_epoch,
            lr_schedule_values=lr_schedule_values, wd_schedule_values=wd_schedule_values,
            num_training_steps_per_epoch=num_training_steps_per_epoch, update_freq=args.update_freq,
            use_amp=args.use_amp, mask=mask,T=args.T,hard=args.hard,args=args,MGDloss=MGD_loss
        )
        if args.output_dir and args.save_ckpt:
            if (epoch + 1) % args.save_ckpt_freq == 0 or epoch + 1 == args.epochs:
                utils.save_model(
                    args=args, model=model_convnext, model_without_ddp=model_convnext_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch, model_ema=model_ema)
        if data_loader_val is not None:
            test_stats = evaluate(data_loader_val, model_convnext, device, use_amp=args.use_amp)
            print(f"Accuracy of the model on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
            if max_accuracy < test_stats["acc1"]:
                max_accuracy = test_stats["acc1"]
                if args.output_dir and args.save_ckpt:
                    utils.save_model(
                        args=args, model=model_convnext, model_without_ddp=model_convnext_without_ddp, optimizer=optimizer,
                        loss_scaler=loss_scaler, epoch="best", model_ema=model_ema)
            print(f'Max accuracy: {max_accuracy:.2f}%')

            if log_writer is not None:
                log_writer.update(test_acc1=test_stats['acc1'], head="perf", step=epoch)
                log_writer.update(test_acc5=test_stats['acc5'], head="perf", step=epoch)
                log_writer.update(test_loss=test_stats['loss'], head="perf", step=epoch)

            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         **{f'test_{k}': v for k, v in test_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}

            # repeat testing routines for EMA, if ema eval is turned on
            if args.model_ema and args.model_ema_eval:
                test_stats_ema = evaluate(data_loader_val, model_ema.ema, device, use_amp=args.use_amp)
                print(f"Accuracy of the model EMA on {len(dataset_val)} test images: {test_stats_ema['acc1']:.1f}%")
                if max_accuracy_ema < test_stats_ema["acc1"]:
                    max_accuracy_ema = test_stats_ema["acc1"]
                    if args.output_dir and args.save_ckpt:
                        utils.save_model(
                            args=args, model=model_convnext, model_without_ddp=model_convnext_without_ddp, optimizer=optimizer,
                            loss_scaler=loss_scaler, epoch="best-ema", model_ema=model_ema)
                    print(f'Max EMA accuracy: {max_accuracy_ema:.2f}%')
                if log_writer is not None:
                    log_writer.update(test_acc1_ema=test_stats_ema['acc1'], head="perf", step=epoch)
                log_stats.update({**{f'test_{k}_ema': v for k, v in test_stats_ema.items()}})
        else:
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                         'epoch': epoch,
                         'n_parameters': n_parameters}

        if args.output_dir and utils.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

        if wandb_logger:
            wandb_logger.log_epoch_metrics(log_stats)

    if wandb_logger and args.wandb_ckpt and args.save_ckpt and args.output_dir:
        wandb_logger.log_checkpoints()


    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))

if __name__ == '__main__':

    parser = argparse.ArgumentParser('SLaK training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
