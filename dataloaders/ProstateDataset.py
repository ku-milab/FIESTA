# Dataloader for prostate datasets
import glob
import numpy as np
import dataloaders.niftiio as nio
import dataloaders.transform_utils as trans
import torch
import os
import platform
import torch.utils.data as torch_data
from functools import partial
from .location_scale_augmentation import FIESTA
import math
import itertools
from .abd_dataset_utils import get_normalize_op
from pdb import set_trace

hostname = platform.node()
BASEDIR='/DataCommon/ksoh/domain_generalization/SLAug/data/prostate/processed/'
# BASEDIR='/DataCommon/ksoh/domain_generalization/DG_data/prostate/processed/'
print(f'Running on machine {hostname}, using dataset from {BASEDIR}')
LABEL_NAME = ["bg", "prostate"]
from dataloaders.niftiio import read_nii_bysitk

class mean_std_norm(object):
    def __init__(self,mean=None,std=None):
        self.mean=mean
        self.std=std

    def __call__(self,x_in):
        if self.mean is None:
            return (x_in-x_in.mean())/x_in.std()
        else:
            return (x_in-self.mean)/self.std

def get_normalize_op(fids, domain=False):
    def get_statistics(scan_fids):
        total_val = 0
        n_pix = 0
        for fid in scan_fids:
            in_img = read_nii_bysitk(fid)
            total_val += in_img.sum()
            n_pix += np.prod(in_img.shape)
            del in_img
        meanval = total_val / n_pix

        total_var = 0
        for fid in scan_fids:
            in_img = read_nii_bysitk(fid)
            total_var += np.sum((in_img - meanval) ** 2 )
            del in_img
        var_all = total_var / n_pix

        global_std = var_all ** 0.5

        return meanval, global_std
    if not domain:
        return mean_std_norm()
    else:
        _mean, _std = get_statistics(fids)
        return mean_std_norm(_mean, _std)

class ProstateDataset(torch_data.Dataset):
    def __init__(self,  mode, transforms, base_dir, domains: list,  idx_pct = [0.7, 0.1, 0.2],  tile_z_dim = 3, extern_norm_fn = None, location_scale=False):
        super(ProstateDataset, self).__init__()
        self.transforms=transforms
        self.is_train = True if mode == 'train' else False
        self.phase = mode
        self.domains = domains if isinstance(domains, list) else [_dm for _dm in domains]
        self.all_label_names = LABEL_NAME
        self.nclass = len(LABEL_NAME)
        self.tile_z_dim = tile_z_dim
        self._base_dir = base_dir
        self.idx_pct = idx_pct

        self.img_pids = {}
        for _domain in self.domains: # load file names
            self.img_pids[_domain] = sorted([ fid.split("_")[-1].split(".nii.gz")[0] for fid in glob.glob(self._base_dir + "/" +  _domain  + "/image_*.nii.gz") ], key = lambda x: int(x))

        self.scan_ids = self.__get_scanids(mode, idx_pct) # patient ids of the entire fold
        print(f'Mode {mode}, use scan ids as follows:')
        print(self.scan_ids)
        self.info_by_scan = None
        self.sample_list = self.__search_samples(self.scan_ids)
        self.pid_curr_load = self.scan_ids
        assert extern_norm_fn is None
        self.normalize_op = lambda x: (x - x.mean()) * 1.0 / x.std()
        # self.normalize_op = extern_norm_fn([ itm['img_fid'] for _, itm in self.sample_list[self.domains[0]].items() ])
        self.actual_dataset = self.__read_dataset()
        self.size = len(self.actual_dataset)
        if location_scale:
            print(f'Applying FIESTA method on {mode} split')
            self.location_scale = FIESTA(vrange=(0.,1.), background_threshold=0.01)
        else:
            self.location_scale = None

    def __get_scanids(self, mode, idx_pct):
        """
        index by domains given that we might need to load multi-domain data
        idx_pct: [0.7 0.1 0.2] for train val test. with order te val tr
        """
        tr_ids      = {}
        val_ids     = {}
        te_ids      = {}
        te_all_ids  = {}

        for _domain in self.domains:
            dset_size   = len(self.img_pids[_domain])
            tr_size     = round(dset_size * idx_pct[0])
            val_size    = math.floor(dset_size * idx_pct[1])
            te_size     = dset_size - tr_size - val_size

            te_ids[_domain]     = self.img_pids[_domain][: te_size]
            val_ids[_domain]    = self.img_pids[_domain][te_size: te_size + val_size]
            tr_ids[_domain]     = self.img_pids[_domain][te_size + val_size: ]
            te_all_ids[_domain] = list(itertools.chain(tr_ids[_domain], te_ids[_domain], val_ids[_domain]   ))

        if self.phase == 'train':
            return tr_ids
        elif self.phase == 'val':
            return val_ids
        elif self.phase == 'test':
            return te_ids
        elif self.phase == 'test_all':
            return te_all_ids

    def __search_samples(self, scan_ids):
        """search for filenames for images and masks
        """
        out_list = {}
        for _domain, id_list in scan_ids.items():
            out_list[_domain] = {}
            for curr_id in id_list:
                curr_dict = {}

                _img_fid = os.path.join(self._base_dir, _domain   ,f'image_{curr_id}.nii.gz')
                _lb_fid  = os.path.join(self._base_dir, _domain   ,f'label_{curr_id}.nii.gz')

                curr_dict["img_fid"] = _img_fid
                curr_dict["lbs_fid"] = _lb_fid
                out_list[_domain][str(curr_id)] = curr_dict

        return out_list

    def __read_dataset(self):
        """
        Build index pointers to individual slices
        Also keep a look-up table from scan_id, slice to index
        """
        out_list = []
        self.info_by_scan = {} # meta data of each scan
        glb_idx = 0 # global index of a certain slice in a certain scan in entire dataset

        for _domain, _sample_list in self.sample_list.items():
            for scan_id, itm in _sample_list.items():
                if scan_id not in self.pid_curr_load[_domain]:
                    continue

                img, _info = nio.read_nii_bysitk(itm["img_fid"], peel_info = True) # get the meta information out
                self.info_by_scan[_domain + '_' + scan_id] = _info

                img = np.float32(img)
                vol_info = {'vol_vmin': img.min(), 'vol_vmax': img.max(), 'vol_mean': img.mean(), 'vol_std': img.std()}
                img = self.normalize_op(img)

                lb = nio.read_nii_bysitk(itm["lbs_fid"])
                lb = np.float32(lb)

                img     = np.transpose(img, (1,2,0))
                lb      = np.transpose(lb, (1,2,0))

                assert img.shape[-1] == lb.shape[-1]

                # now start writing everthing in
                # write the beginning frame
                out_list.append( {"img": img[..., 0: 1],
                               "lb":lb[..., 0: 0 + 1],
                               "is_start": True,
                               "is_end": False,
                               "domain": _domain,
                               "nframe": img.shape[-1],
                               "scan_id": _domain + "_" + scan_id,
                               "z_id":0,
                               "vol_info":vol_info})
                glb_idx += 1

                for ii in range(1, img.shape[-1] - 1):
                    out_list.append( {"img": img[..., ii: ii + 1],
                               "lb":lb[..., ii: ii + 1],
                               "is_start": False,
                               "is_end": False,
                               "nframe": -1,
                               "domain": _domain,
                               "scan_id":_domain + "_" + scan_id,
                               "z_id": ii,
                               "vol_info":vol_info})
                    glb_idx += 1

                ii += 1 # last frame, note the is_end flag
                out_list.append( {"img": img[..., ii: ii + 1],
                               "lb":lb[..., ii: ii+ 1],
                               "is_start": False,
                               "is_end": True,
                               "nframe": -1,
                               "domain": _domain,
                               "scan_id":_domain + "_" + scan_id,
                               "z_id": ii,
                               "vol_info":vol_info})
                glb_idx += 1

        return out_list

    def __getitem__(self, index):
        index = index % len(self.actual_dataset)
        curr_dict = self.actual_dataset[index]
        if self.is_train:
            if self.location_scale is not None:
                img = curr_dict["img"].copy()
                lb = curr_dict["lb"].copy()
                img = self.denorm_(img, curr_dict['vol_info'])

                CFAT = self.location_scale.FourierAugmentativeTransformer(img.copy())
                CFAT = self.renorm_(CFAT, curr_dict['vol_info'])

                LFAT_pre = self.location_scale.Local_Location_Scale_Augmentation(img.copy(), lb.astype(np.int32))
                LFAT = self.location_scale.FourierAugmentativeTransformer(LFAT_pre)
                LFAT = self.renorm_(LFAT, curr_dict['vol_info'])
                comp = np.concatenate([CFAT, LFAT, curr_dict["lb"]], -1)
                if self.transforms:
                    timg, lb = self.transforms(comp, c_img=2, c_label=1, nclass=self.nclass, is_train=self.is_train,
                                               use_onehot=False)
                    CFAT, LFAT = np.split(timg, 2, -1)
                img = CFAT

                aug_img = LFAT
                aug_img = np.float32(aug_img)
                aug_img = np.transpose(aug_img, (2, 0, 1))
                aug_img = torch.from_numpy(aug_img)
            else:
                comp = np.concatenate([curr_dict["img"], curr_dict["lb"]], axis=-1)
                if self.transforms:
                    img, lb = self.transforms(comp, c_img=1, c_label=1, nclass=self.nclass, is_train=self.is_train,
                                              use_onehot=False)
                aug_img = 1
        else:
            img = curr_dict['img']
            lb = curr_dict['lb']
            aug_img = 1

        img = np.float32(img)
        lb = np.float32(lb)

        img = np.transpose(img, (2, 0, 1))
        lb  = np.transpose(lb, (2, 0, 1))

        img = torch.from_numpy( img )
        lb  = torch.from_numpy( lb )

        if self.tile_z_dim > 1:
            img = img.repeat( [ self.tile_z_dim, 1, 1] )
            assert img.ndimension() == 3

        is_start    = curr_dict["is_start"]
        is_end      = curr_dict["is_end"]
        nframe      = np.int32(curr_dict["nframe"])
        scan_id     = curr_dict["scan_id"]
        z_id        = curr_dict["z_id"]

        sample = {"images": img,
                "labels":lb[0].long(),
                "is_start": is_start,
                "is_end": is_end,
                "nframe": nframe,
                "scan_id": scan_id,
                "z_id": z_id,
                "aug_images": aug_img,
                }
        return sample

    def denorm_(self, img, vol_info):
        # scale to 0 - 1
        vmin, vmax, vmean, vstd = vol_info['vol_vmin'], vol_info['vol_vmax'], vol_info['vol_mean'], vol_info['vol_std']
        return ((img * vstd + vmean) - vmin) / (vmax - vmin)

    def renorm_(self, img, vol_info):
        vmin, vmax, vmean, vstd = vol_info['vol_vmin'], vol_info['vol_vmax'], vol_info['vol_mean'], vol_info['vol_std']
        return ((img * (vmax - vmin) + vmin) - vmean) / vstd

    def __len__(self):
        """
        copy-paste from basic naive dataset configuration
        """
        return len(self.actual_dataset)

tr_func  = trans.transform_with_label(trans.tr_aug)

def get_training(modality, location_scale, idx_pct = [0.7, 0.1, 0.2], tile_z_dim = 3):
    return ProstateDataset(idx_pct = idx_pct,\
        mode = 'train',\
        domains = modality,\
        transforms = tr_func,\
        base_dir = BASEDIR,\
        extern_norm_fn = None,
        tile_z_dim = tile_z_dim,
       location_scale=location_scale)

def get_validation(modality, idx_pct = [0.7, 0.1, 0.2], tile_z_dim = 3):
     return ProstateDataset(idx_pct = idx_pct,\
        mode = 'val',\
        transforms = None,\
        domains = modality,\
        base_dir = BASEDIR,\
        extern_norm_fn = None,\
        tile_z_dim = tile_z_dim)

def get_test(modality, tile_z_dim = 3, idx_pct = [0.7, 0.1, 0.2]):
     return ProstateDataset(idx_pct = idx_pct,\
        mode = 'test',\
        transforms = None,\
        domains = modality,\
        extern_norm_fn = None,\
        base_dir = BASEDIR,\
        tile_z_dim = tile_z_dim)

def get_test_all(modality, tile_z_dim = 3, idx_pct = [0.7, 0.1, 0.2]):
     return ProstateDataset(idx_pct = idx_pct,\
        mode = 'test_all',\
        transforms = None,\
        domains = modality,\
        extern_norm_fn = None,\
        base_dir = BASEDIR,\
        tile_z_dim = tile_z_dim)

def get_test_exclu(tr_modality, tile_z_dim = 3, idx_pct = [0.7, 0.1, 0.2]):
    modality = [ md for md in ['A', 'B', 'C', 'D', 'E', 'F'] if md != tr_modality ]
    return ProstateDataset(idx_pct = idx_pct,\
        mode = 'test_all',\
        domains = modality,\
        transforms = None,\
        base_dir = BASEDIR,\
        extern_norm_fn = None,
        tile_z_dim = tile_z_dim)

