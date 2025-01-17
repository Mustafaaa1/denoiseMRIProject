from model.unet_model import UNet
from model.attention_unet import Atten_Unet 
from model.unet_MultiDecoder import UNet_MultiDecoders
from torch.utils.data import DataLoader, random_split
from utils import pre_data, patientDataset
from pathlib import Path
import os
import numpy as np
import torch
import argparse
import wandb
result_path = Path('./results/')

def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images')
    parser.add_argument('--load', '-f', type=str, default='../checkpoints/checkpoint_epoch30.pth',
                        help='Load the model to test the result')

    return parser.parse_args()

def to_numpy(*argv):
    """
    Convert the parameters from tensor to numpy
    """
    params = []
    for arg in argv:
        assert torch.is_tensor(arg), 'This is not a tensor'
        params.append(arg.cpu().detach().numpy())
    return params

def save_params(results):
    """
    save the parameters maps and M as numpy array
    """
    Path(result_path).mkdir(parents=True, exist_ok=True)
    for key, res in results.items():
        np.save(os.path.join(result_path, key), res)

if __name__ == '__main__':

    """
    This file load the trained model and run it on one patient, and 
    saves the result in ./results/
    """

    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

    args = get_args()

    test_dir = '../PredictFolder'
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # Load the test dataset
    test = patientDataset(test_dir)
    test_loader = DataLoader(test, batch_size=22, shuffle=False, num_workers=4)
    test_b0 = test.pre.image_b0()

    # Initialize the b values [100, 200, 300, ..., 2000]
    b = torch.linspace(0, 2000, steps=21, device=device)
    b = b[1:]
    
    # Load the UNet model
    net = Atten_Unet(n_channels=20, b=b, rice=False, bilinear=False)
    net.load_state_dict(torch.load(args.load, map_location=device))
    net.to(device=device)

    total_loss = 0
    net.eval()
    experiment = wandb.init(project="ResultFromTraining")

    with torch.no_grad():
        for i,X in enumerate(test_loader):
            images = X.to(device=device, dtype=torch.float32)
            
            mse = torch.nn.MSELoss()
            M, d_1, d_2, f, sigma = net(images)
            loss = mse(M, images)
            total_loss += loss.item()
            experiment.log({'prediction': wandb.Image(M[0, 15, :, :], caption=f'patient {i}'),
                            'image': wandb.Image(images[0, 15, :, :], caption=f'patient {i}')})

    print("Test Loss: {}".format(total_loss / len(test_loader)))

    M, d_1, d_2, f, sigma = to_numpy(M, d_1, d_2, f, sigma)

    results = {'M.npy': M, 'd1.npy': d_1, 
                'd2.npy': d_2, 'f.npy': f, 'sigma_g.npy': sigma, 'b0': test_b0[0], 'images.npy':images.detach().cpu().numpy()}
    
    # save the physical parameters and denoised images
    save_params(results)
