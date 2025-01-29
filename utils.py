from cmath import sqrt
from torch.utils.data import Dataset
import torch
import os
from scipy import special
import numpy as np
import torch.nn as nn
import torchvision
from torchvision import transforms
from IPython import embed
from pytorch_msssim import MS_SSIM
class pre_data():
    """
    This class load the data from the pointed directory
    :"""

    def __init__(self, data_dir):
        """
        data_dir: directory storing the data 'save_npy'
        """
        super().__init__()
        self.data_dir = data_dir
        self.names = self.pat_names()

    def load(self, pat_path):
        """
        Load the data from the patient_path
        """
        data_path = os.path.join(self.data_dir, pat_path)
        pat_data = np.load(data_path, allow_pickle=True)[()]

        return pat_data
    
    def pat_names(self):
        """
        Save the patients' name in a list. e.g. ['pat1', 'pat2', ..., ...]
        """
        return [pat_d[:-4] for pat_d in os.listdir(self.data_dir)]

    def image_data(self, pat_path, slice_idx, dir: int, input_sigma: bool, normalize=True, crop=True):
        """
        Get the image data of the corresponding diffusion direction (slices as batch size) 
        
        INPUT:
        pat_path - string- the path to the directory of the patient data
        slice_idx - the index of the slice
        dir - int - diffusion direction: 0,1,2
        normalize - boolean - if the image data is normalzied by its corresponding b0
        crop - boolean - if cropping the irrelevant background

        return:
        images - torch array: (1, 20, h, w) 20 is the number of diffusion direction
        """
        
        data = self.load(pat_path)
        idx = slice_idx

        #The data is saved as a dictionary with keys 'image_data' and 'image_b0'
        sigma = 0
        #sigma_max = 1
        # image_data - (num_slices,num_diffsuion_direction , H, W)
        image_data = data['image']['3Dsig'][idx,:,:,:]


      # image_b0 - (num_slices, H, W)
        image_b0 = data['image_b0'][idx, :, :]

        image_data = image_data.astype('float32')
        image_b0 = image_b0.astype('float32')
        image_data = torch.from_numpy(image_data) #torch.tensor(image_data,dtype=torch.float32)
        image_b0 = torch.from_numpy(image_b0) #torch.tensor(image_b0, dtype=torch.float32)

        if input_sigma:
            sigma = data['result']['3Dsig'][idx, :, :, 10]
            sigma = sigma.astype('float32')
            sigma = torch.from_numpy(sigma)  # torch.tensor(sigma, dtype=torch.float32)
        else: sigma = torch.tensor([1])
        if dir ==0:

            image_data = image_data[0:20,:,:]
        elif dir ==1:
            image_data = image_data[20:40,:,:]

        elif dir ==2:
            image_data = image_data[40:60,:,:]

        else:
            print('ERROR: dir index is not 0,1 or 2')


        #if normalize:
        #    image_b0[image_b0 == 0] = 1
        #else:
        #    image_b0 = 1
        factor = torch.max(image_data)
        # (num_diffusion_direction, h, w)


        image_data = image_data / factor




        #imgs = torch.from_numpy(image_dir)

        image_b0 = image_b0.unsqueeze(dim=0)
        if input_sigma:
            sigma = sigma / factor
            sigma = sigma.unsqueeze(dim=0)

        # crop the redundant pixels
        if crop:
            image_data = self.crop_image(image_data)
            if input_sigma:
                sigma = self.crop_image(sigma)
            image_b0 = self.crop_image(image_b0)


        ###means = imgs.view(imgs.shape[0], -1).mean(dim=1)
        #maxs,_ = imgs.view(imgs.shape[0], -1).max(dim=1)
        ###stds = imgs.view(imgs.shape[0], -1).std(dim=1)
        #imgs = imgs/maxs.unsqueeze(1).unsqueeze(1)
        #norm = transforms.Normalize(means, stds)
        #out = norm(imgs)

        return image_data,image_b0, sigma, factor  #,means,stds
    
    def image_b0(self):
        """
        Get the b0 for all the patients
        """
        files = os.listdir(self.data_dir)
        
        if not isinstance(files, list):
            files = [files]

        data = [self.load(file) for file in files]
        b0s = [pat_data['image_b0'] for pat_data in data]
     
        #b0 for normalization
        return [np.where(b0 == 0, 1, b0) for b0 in b0s]

    def crop_image(self, images):
        """
        (20, H, W)
        """
        return images[:, 20:-20, :]

class post_processing():
    """
    This class include the post processing function to evaluate the trained model
    """
    def __init__(self):
        super().__init__()
    
    def evaluate(self, val_loader, net, rank, b, input_sigma: bool):
        """
        evlaute the performance of network 
        """
        criterion = MS_SSIM(channel=20, win_size=5)
        criterion2 = nn.MSELoss()
        net.eval()
        val_losses = 0

        params_val = dict()
              #batch,_,_
        final_sigma = 0
        for images,image_b0,sigma,scale_factor in val_loader:

            images = images.to(rank, dtype=torch.float32, non_blocking=True)
            sigma = sigma.to(rank, dtype=torch.float32, non_blocking=True)
            image_b0 = image_b0.to(rank, dtype=torch.float32, non_blocking=True)
            scale_factor = scale_factor.to(rank, dtype=torch.float32, non_blocking=True)
            M, d1, d2, f,sigma_out = net(images,b,image_b0, sigma, scale_factor)
            M = M * scale_factor.view(-1, 1, 1, 1)
            images = images * scale_factor.view(-1, 1, 1, 1)
            criterion.data_range = torch.max(images)
            ssim_loss = 1-criterion(M, images)
            mse_loss = criterion2(M,images)
            loss = ssim_loss * mse_loss  #+ssim_loss
            loss_value = torch.tensor(loss.item())


            params_val = {'d1':d1, 'd2': d2, 'f': f}

            if input_sigma:
                final_sigma = sigma[0,0,:,:]
            else:
                final_sigma  =sigma_out[0,0,:,:]


            val_losses += loss_value

        return val_losses/len(val_loader), params_val, M[0, 0, :, :], images[0, 0, :, :], final_sigma

class patientDataset(Dataset):
    '''
    wrap the patient numpy data to be dealt by the dataloader
    '''
    def __init__(self, data_dir, input_sigma: bool, transform=None, num_slices=22, normalize = True, custom_list = None):
        super(Dataset).__init__()
        self.data_dir = data_dir
        self.transform = transform
        self.num_slices = 22
        self.num_direction = 3
        self.input_sigma = input_sigma

        # Must not include ToTensor()!
        if custom_list is not None:
            self.patients = custom_list
        else:
            self.patients = os.listdir(data_dir)
        self.pre = pre_data(data_dir)
        self.normalize = normalize

    def __len__(self):
        """each data file consist of 22 slices"""
        return len(self.patients)*self.num_slices*self.num_direction
    
    def __getitem__(self, idx):
        # each time read on sample
        if torch.is_tensor(idx):
            idx = idx.tolist()
        direction_indice = idx//(self.num_slices*len(self.patients))
        pats_indice = idx // (self.num_slices*self.num_direction)
        slice_indice = idx % self.num_slices

        #imgs,means,stds
        imgs,b0_data, sigma, factor = self.pre.image_data(self.patients[pats_indice], slice_indice, direction_indice,self.input_sigma, normalize=self.normalize)
        
        if self.transform:
            imgs = self.transform(imgs)

        return imgs,b0_data, sigma, factor#,means,stds

def init_weights(model):
    for name, module in model.named_modules():
        # Apply He initialization to Conv2d layers with ReLU activations
        if isinstance(module, nn.Conv2d):
            if 'att' in name:  # Attention layers
                nn.init.xavier_uniform_(module.weight)  # Xavier initialization for Sigmoid layers
            else:  # Conv2d layers using ReLU
                nn.init.kaiming_uniform_(module.weight, nonlinearity='relu')

        # Apply Xavier initialization to BatchNorm layers
        elif isinstance(module, nn.BatchNorm2d):
            nn.init.constant_(module.weight, 1)  # Set the weight of BatchNorm to 1
            nn.init.constant_(module.bias, 0)  # Set the bias of BatchNorm to 0

        # Apply Xavier initialization for Linear layers if any (you may not have any in your current structure)
        elif isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight)