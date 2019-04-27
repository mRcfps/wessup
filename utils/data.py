import csv
import os
import random
import glob

import numpy as np
from PIL import Image

import torch
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms.functional as TF

import config
from .tile import pad_image
from .tile import compute_patches_grid_shape
from .preprocessing import segment_superpixels


def _list_images(path):
    """Glob all images within a directory."""

    images = []
    for ext in ('jpg', 'jpeg', 'png', 'bmp'):
        images.extend(glob.glob(os.path.join(path, f'*.{ext}')))
    return images


def _transform_and_crop(img, mask=None):
    """
    Simultaneously apply random transformations to images (and optionally masks).
    """

    if random.random() > 0.5:
        img = TF.hflip(img)
        mask = TF.hflip(mask) if mask else None

    if random.random() > 0.5:
        img = TF.vflip(img)
        mask = TF.vflip(mask) if mask else None

    # possibly some rotations ...

    patch_size = config.PATCH_SIZE
    up = random.randint(0, img.height - patch_size)
    left = random.randint(0, img.width - patch_size)
    img = TF.crop(img, up, left, patch_size, patch_size)
    mask = TF.crop(mask, up, left, patch_size, patch_size) if mask else None

    if mask is None:
        return img, (up, left)

    return (img, mask), (up, left)


class FullAnnotationDataset(Dataset):
    """Segmentation dataset with mask-level (full) annotation."""

    def __init__(self, root_dir, train=True):
        self.img_paths = _list_images(os.path.join(root_dir, 'images'))
        self.mask_paths = _list_images(os.path.join(root_dir, 'masks'))
        self.train = train

        patch_area = config.PATCH_SIZE ** 2
        img = Image.open(self.img_paths[0])
        img_area = img.height * img.width
        self.patches_per_img = int(np.round(img_area / patch_area))

    def __len__(self):
        return len(self.img_paths) * self.patches_per_img

    def __getitem__(self, idx):
        idx = idx // self.patches_per_img
        img = Image.open(self.img_paths[idx])
        mask = Image.open(self.mask_paths[idx])

        if self.train:
            (img, mask), _ = _transform_and_crop(img, mask)

        sp_maps, sp_labels = segment_superpixels(img, mask)

        # convert to tensors
        img = TF.to_tensor(img)
        mask = torch.LongTensor(np.array(mask))
        sp_maps = torch.Tensor(sp_maps)
        sp_labels = torch.LongTensor(sp_labels)

        return img, mask, sp_maps, sp_labels


class DotAnnotationDataset(Dataset):
    """Segmentation dataset with dot annotation."""

    def __init__(self, root_dir):
        self.img_paths = _list_images(os.path.join(root_dir, 'images'))
        self.label_paths = glob.glob(os.path.join(root_dir, 'labels', '*.csv'))

        patch_area = config.PATCH_SIZE ** 2
        img = Image.open(self.img_paths[0])
        img_area = img.height * img.width
        self.patches_per_img = int(np.round(img_area / patch_area))

    def __len__(self):
        return len(self.img_paths) * self.patches_per_img

    def __getitem__(self, idx):
        idx = idx // self.patches_per_img
        img, (up, left) = _transform_and_crop(Image.open(self.img_paths[idx]))

        with open(self.label_paths[idx]) as fp:
            reader = csv.reader(fp)
            label = np.array([[int(d) for d in point] for point in reader])

        # subtract offsets from top and left
        label[:, 0] -= up
        label[:, 1] -= left

        sp_maps, sp_labels = segment_superpixels(img, label)

        # convert to tensors
        img = TF.to_tensor(img)
        sp_maps = torch.Tensor(sp_maps)
        sp_labels = torch.LongTensor(sp_labels)

        # the second return value is the missing mask for convenience
        return img, sp_maps, sp_labels


class WholeImageDataset(Dataset):
    """Dataset with whole size images."""

    def __init__(self, root_dir):
        self.img_paths = _list_images(os.path.join(root_dir, 'images'))

        if os.path.exists(os.path.join(root_dir, 'masks')):
            self.masks = [
                np.array(Image.open(mask_path))
                for mask_path in _list_images(os.path.join(root_dir, 'masks'))
            ]
        else:
            self.masks = None

        # patches grid shape for each image
        self.patches_grids = [
            compute_patches_grid_shape(np.array(Image.open(img_path)),
                                       config.PATCH_SIZE, config.INFER_STRIDE)
            for img_path in self.img_paths
        ]

        # number of patches for each image
        self.patches_nums = [n_h * n_w for n_h, n_w in self.patches_grids]

        # sequence for identifying image index from patch index
        self.patches_numseq = np.cumsum(self.patches_nums)

    def __len__(self):
        return sum(self.patches_nums)

    def __getitem__(self, patch_idx):
        img_idx = self.patch2img(patch_idx)
        img = np.array(Image.open(self.img_paths[img_idx]))
        img = pad_image(img, config.PATCH_SIZE, config.INFER_STRIDE)

        if img_idx > 0:
            # patch index WITHIN this image
            patch_idx -= self.patches_numseq[img_idx - 1]

        _, n_w = self.patches_grids[img_idx]
        up = (patch_idx // n_w) * config.INFER_STRIDE
        left = (patch_idx % n_w) * config.INFER_STRIDE

        patch = img[up:up + config.PATCH_SIZE, left:left + config.PATCH_SIZE]

        return TF.to_tensor(patch), torch.Tensor(segment_superpixels(patch))

    def patch2img(self, patch_idx):
        """Identify which image this patch belongs to."""

        return np.argmax(self.patches_numseq > patch_idx)


def get_trainval_dataloaders(root_dir, num_workers):
    """Returns training and validation dataloaders."""

    train_dir = os.path.join(root_dir, 'train')
    val_dir = os.path.join(root_dir, 'val')

    # if `labels` directory is present in `root_dir`, then it's dot annotation mode
    is_dot_anno = os.path.exists(os.path.join(train_dir, 'labels'))

    datasets = {
        'train': DotAnnotationDataset(train_dir) if is_dot_anno else FullAnnotationDataset(train_dir),
        'val': FullAnnotationDataset(val_dir, train=False),
    }

    dataloaders = {
        'train': DataLoader(datasets['train'], batch_size=1,
                            shuffle=True, num_workers=num_workers),
        'val': DataLoader(datasets['val'], batch_size=1,
                          shuffle=True, num_workers=num_workers),
    }

    return dataloaders
