import argparse
import glob
import json
import os
import random
import re
from importlib import import_module
from pathlib import Path

import warnings
warnings.filterwarnings("ignore")

from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

import numpy as np
import matplotlib.pyplot as plt
import torch
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import Subset, DataLoader,WeightedRandomSampler
from torch.utils.tensorboard import SummaryWriter

from catalyst import data as data_catal


from dataset import MaskBaseDataset
from loss import create_criterion
from torchsampler import ImbalancedDatasetSampler
from utils import *
from triplet_loss import *
import time

#Mixed precision
from torch.cuda.amp import GradScaler , autocast

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"


def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    save_dir = increment_path(os.path.join(model_dir, args.name))

    # -- settings
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- dataset
    dataset_module = getattr(import_module("dataset"), args.dataset)  # default: BaseAugmentation
    dataset = dataset_module(
        data_dir=data_dir,
        age_filter = args.agefilter
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(import_module("dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std
    )
    dataset.set_transform(transform)

    # -- data_loader
    train_set, val_set = dataset.split_dataset()

    
    if args.sampler == "Imbalanced":

        train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            shuffle=False,
            pin_memory=use_cuda,
            drop_last=False,
            sampler = ImbalancedDatasetSampler(train_set)
        )

    elif args.sampler == 'Weighted':
        print("Weighted")
        all_label_ids = train_set.dataset.get_all_labels()
        labels_unique , counts = np.unique(all_label_ids, return_counts = True)
        class_weights = [sum(counts) / c for c in counts]
        example_weights = [class_weights[e] for e in all_label_ids]
        train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            shuffle=False,
            pin_memory=use_cuda,
            drop_last=False,
            sampler = WeightedRandomSampler(example_weights , len(all_label_ids))
        )
        
    elif args.sampler == "Dynamic":
        sampler = data_catal.DynamicBalanceClassSampler(labels=train_set.dataset.get_all_labels())
        train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            shuffle=False,
            pin_memory=use_cuda,
            drop_last=False,
            sampler = sampler
        )
        
    elif args.sampler == 'Balance':
        sampler = data_catal.BalanceBatchSampler(labels=train_set.dataset.get_all_labels() , p = 8 , k = 16)
        train_loader = DataLoader(
            train_set,
            batch_size = sampler.batch_size,
            num_workers = args.num_workers,
            shuffle = False,
            pin_memory = use_cuda,
            drop_last = False,
            sampler = sampler
        )        
    
    else :
        train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            shuffle=True,
            pin_memory=use_cuda,
            drop_last=False,
        )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=args.num_workers,
        shuffle=False,
        pin_memory=use_cuda,
        drop_last=False,
    )
    

    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: BaseModel
    model = model_module(
        num_classes=num_classes
    ).to(device)
    model.load_state_dict(torch.load(model_path, map_location=device))
    model = torch.nn.DataParallel(model)



    # -- loss & metric
    criterion = create_criterion(args.criterion)  # default: cross_entropy
    opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: SGD
    optimizer = opt_module(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.lr,
        weight_decay=5e-4
    )
    scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)
    scaler = GradScaler()

    # -- logging
    logger = SummaryWriter(log_dir=save_dir)
    with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)
    
    early_stop = 0
    best_val_f1 = 0
    best_val_acc = 0
    best_val_loss = np.inf
    
    for epoch in range(args.epochs):
        train_start_time = time.time()
        if early_stop >= args.num_early_stop : break
        # train loop
        model.train()
        loss_value = 0
        matches = 0
        f1_value = 0
        for idx, train_batch in enumerate(train_loader):
            inputs, labels = train_batch
            inputs = inputs.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()    
            
            r = np.random.rand(1)
            if r > args.Cbeta and args.CutMix > 0 :
                target_a , target_b , lam = generate_cutmix_image(inputs , labels , args.CutMix)
                with autocast():
                    outs = model(inputs)
                    preds = torch.argmax(outs, dim=-1)
                    loss = criterion(outs, target_a) * lam + criterion(outs, target_b) * (1. - lam)

            if r > args.Mbeta and args.MixUp > 0 :
                inputs, target_a, target_b, lam = mixup_data(inputs, labels, args.MixUp)
                with autocast():
                    outs = model(inputs)
                    preds = torch.argmax(outs, dim=-1)
                    loss = criterion(outs, target_a) * lam + criterion(outs, target_b) * (1. - lam)

            else :
                with autocast():
                    outs = model(inputs)
                    preds = torch.argmax(outs, dim=-1)
                    loss = criterion(outs, labels)


            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            
            loss_value += loss.item()
            matches += (preds == labels).sum().item()
            f1_value += f1_score(labels.data.detach().cpu(), preds.detach().cpu(), average='macro')
            
            if (idx + 1) % args.log_interval == 0:

                train_end_time = time.time()
                train_loss = loss_value / args.log_interval
                train_acc = matches / args.batch_size / args.log_interval
                train_f1 = f1_value / args.log_interval

                current_lr = get_lr(optimizer)
                print(
                    f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || training_time : {train_end_time - train_start_time :4.2f} || "
                    f"training loss {train_loss:4.4} || training accuracy {train_acc:4.2%} || training f1_score {train_f1:4.3} || lr {current_lr}"
                )

                logger.add_scalar("Train/loss", train_loss, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/accuracy", train_acc, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/f1", train_f1, epoch * len(train_loader) + idx)

                loss_value = 0
                matches = 0
                f1_value = 0

                train_start_time = time.time()

        scheduler.step()
        
        torch.save(model.module.state_dict(), f"{save_dir}/epoch{epoch}.pth")

        val loop
        with torch.no_grad():
            print("Calculating validation results...")
            model.eval()
            val_loss_items = []
            val_acc_items = []
            val_f1_items = []
            
            figure = None
            for val_batch in val_loader:
                inputs, labels = val_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                outs = model(inputs)
                preds = torch.argmax(outs, dim=-1)

                loss_item = criterion(outs, labels).item()
                acc_item = (labels == preds).sum().item()
                f1_item = f1_score(labels.data.detach().cpu(), preds.detach().cpu(), average='macro')
                
                val_loss_items.append(loss_item)
                val_acc_items.append(acc_item)
                val_f1_items.append(f1_item)

                if figure is None:
                    inputs_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                    inputs_np = dataset_module.denormalize_image(inputs_np, dataset.mean, dataset.std)
                    figure = grid_image(inputs_np, labels, preds, args.dataset != "MaskSplitByProfileDataset")

            val_loss = np.sum(val_loss_items) / len(val_loader)
            val_acc = np.sum(val_acc_items) / len(val_set)
            val_f1 = np.sum(val_f1_items) / len(val_loader)
            best_val_loss = min(best_val_loss, val_loss)
            best_val_acc = max(best_val_acc , val_acc)
            
            if (epoch+1)%5 == 0:
                torch.save(model.module.state_dict(), f"{save_dir}/epoch{epoch}.pth")
            if val_f1 > best_val_f1:
                print(f"New best model for val accuracy : {val_acc:4.2%}! saving the best model..")
                torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
                best_val_f1 = val_f1
                early_stop = 0
            else : 
                early_stop += 1
            torch.save(model.module.state_dict(), f"{save_dir}/last.pth")
            
            print(
                f"[Val] acc : {val_acc:4.2%} || loss: {val_loss:4.2} || f1: {val_f1 : 4.2} || "
                f"best acc : {best_val_acc:4.2%} || best loss: {best_val_loss:4.2} || best f1 : {best_val_f1:4.3}"
            )
            
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            logger.add_scalar("Val/f1", val_f1, epoch)
            logger.add_figure("results", figure, epoch)
            print()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=42, help='random seed (default: 42)')
    parser.add_argument('--num_workers', type=int, default=4, help='num_workers (default: 4)')
    parser.add_argument('--epochs', type=int, default=50, help='number of epochs to train (default: 50)')
    parser.add_argument('--dataset', type=str, default='MaskSplitByProfileDataset', help='dataset augmentation type (default: MaskSplitByProfileDataset [MaskDataset])')
    parser.add_argument('--agefilter', type=bool, default='True', help='age filter if age >= 58 then age = 60 (default: True)')
    parser.add_argument('--augmentation', type=str, default='BaseAugmentation', help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument('--MixUp', type=float, default=0, help='learning rate (default: 1e-3)')
    parser.add_argument('--CutMix', type=float, default=0,  help='learning rate (default: 1e-3)')
    parser.add_argument('--Mbeta', type=float, default=0,  help='learning rate (default: 1e-3)')
    parser.add_argument('--Cbeta', type=float, default=0,  help='learning rate (default: 1e-3)')
    parser.add_argument("--resize", type = int, default = 224, help='resize size for image when training')
    parser.add_argument('--sampler', type=str, default='None', help='sampler (default: No sampler [Imbalanced , Weighted , Dynamcic, Balance])') # Imbalanced
    parser.add_argument('--batch_size', type=int, default=128, help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=128, help='input batch size for validing (default: 1000)')
    parser.add_argument('--model', type=str, default='RBaseModel', help='model type (default: RBaseModel)[Model_Whale]')
    parser.add_argument('--optimizer', type=str, default='SGD', help='optimizer type (default: SGD)')
    parser.add_argument('--lr', type=float, default=1e-3, help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='cross_entropy', help='criterion type (default: cross_entropy [focal, label_smoothing , f1])')
    parser.add_argument('--lr_decay_step', type=int, default=20, help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--num_early_stop', type=int, default=3, help='early stop number (defalut : 3)')
    parser.add_argument('--log_interval', type=int, default=20, help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='exp', help='model save at {SM_MODEL_DIR}/{name}')
    
    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './model'))

    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)
    